"""
Import service: the DB-aware layer that sits on top of the pure parsers in
``src/ingestion``.

Responsibilities that live here (and deliberately not in the parsers):
  - content-hash based duplicate-file detection
  - upserting Transactions and writing TransactionVersion history
  - recording import metadata (counts, status) on SourceFile

The parsers stay pure (string in, NormalizedRow/RowError out) so they can be
unit tested without any of this.
"""
import hashlib
from datetime import timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ingestion.base import NormalizedRow, ParseResult
from src.ingestion.registry import detect_source_system, get_parser
from src.models import (
    FileStatus,
    Side,
    SourceFile,
    SourceSystem,
    Transaction,
    TransactionVersion,
)


def _hash_content(content_bytes: bytes) -> str:
    return hashlib.sha256(content_bytes).hexdigest()


def _to_naive_utc(dt):
    """SQLite has no timezone-aware datetime type; we normalize everything
    to UTC on write and treat 'naive datetime' as 'UTC' by convention
    everywhere else in the app. See README > Assumptions."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def import_file(
    session: Session,
    filename: str,
    content_bytes: bytes,
    source_system: str | None = None,
) -> SourceFile:
    """
    Import one CSV file. Returns the SourceFile row describing the outcome
    (check ``.status`` — a duplicate or malformed file is *not* raised as an
    exception, it is recorded so the UI can show it).

    ``source_system`` may be given explicitly ("LEDGER" / "STATEMENT"); if
    omitted, the format is auto-detected from the header row.
    """
    content_hash = _hash_content(content_bytes)

    existing = session.execute(
        select(SourceFile).where(SourceFile.content_hash == content_hash)
    ).scalar_one_or_none()

    if existing is not None:
        duplicate_record = SourceFile(
            filename=filename,
            source_system=existing.source_system,
            content_hash=None,  # the real hash stays owned by the original file's row (unique constraint)
            status=FileStatus.DUPLICATE,
            row_count=0,
            error_summary=f"identical content already imported as file #{existing.id} ({existing.filename})",
        )
        session.add(duplicate_record)
        session.flush()
        return duplicate_record

    try:
        csv_text = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        source_file = SourceFile(
            filename=filename,
            source_system=SourceSystem(source_system) if source_system else SourceSystem.LEDGER,
            content_hash=content_hash,
            status=FileStatus.FAILED,
            error_summary="file is not valid UTF-8 text; expected a CSV file",
        )
        session.add(source_file)
        session.flush()
        return source_file

    if source_system is None:
        detected = detect_source_system(csv_text)
        if detected is None:
            source_file = SourceFile(
                filename=filename,
                source_system=SourceSystem.LEDGER,  # placeholder; format unknown
                content_hash=content_hash,
                status=FileStatus.FAILED,
                error_summary="could not detect source format from header row; specify source_system explicitly",
            )
            session.add(source_file)
            session.flush()
            return source_file
        source_system = detected

    parser = get_parser(source_system)
    parse_result: ParseResult = parser.parse(csv_text)

    source_file = SourceFile(
        filename=filename,
        source_system=SourceSystem(source_system),
        content_hash=content_hash,
        row_count=len(parse_result.rows) + len([e for e in parse_result.errors if e.row_number > 0]),
    )
    session.add(source_file)
    session.flush()  # obtain source_file.id

    if parse_result.is_fatal:
        source_file.status = FileStatus.FAILED
        source_file.error_summary = "; ".join(e.message for e in parse_result.errors)
        return source_file

    created = corrected = unchanged = 0
    for row in parse_result.rows:
        outcome = _apply_row(session, source_file, SourceSystem(source_system), row)
        if outcome == "created":
            created += 1
        elif outcome == "corrected":
            corrected += 1
        else:
            unchanged += 1

    failed = len([e for e in parse_result.errors if e.row_number > 0])

    source_file.rows_created = created
    source_file.rows_corrected = corrected
    source_file.rows_unchanged = unchanged
    source_file.rows_failed = failed
    source_file.is_correction = corrected > 0
    source_file.status = FileStatus.PROCESSED_WITH_ERRORS if failed else FileStatus.PROCESSED
    if failed:
        source_file.error_summary = "; ".join(
            f"row {e.row_number}: {e.message}" for e in parse_result.errors if e.row_number > 0
        )

    return source_file


def _row_differs_from_transaction(row: NormalizedRow, tx: Transaction) -> bool:
    return (
        Decimal(tx.quantity) != row.quantity
        or Decimal(tx.price) != row.price
        or Decimal(tx.gross_amount) != row.gross_amount
        or tx.transacted_at != _to_naive_utc(row.transacted_at)
        or tx.state != row.state
        or tx.side.value != row.side
        or tx.instrument != row.instrument
    )


def _apply_row(
    session: Session,
    source_file: SourceFile,
    source_system: SourceSystem,
    row: NormalizedRow,
) -> str:
    """Create or update the Transaction for one normalized row. Returns
    "created", "corrected", or "unchanged"."""
    existing = session.execute(
        select(Transaction).where(
            Transaction.source_system == source_system,
            Transaction.natural_key == row.natural_key,
        )
    ).scalar_one_or_none()

    naive_transacted_at = _to_naive_utc(row.transacted_at)

    if existing is None:
        tx = Transaction(
            source_system=source_system,
            natural_key=row.natural_key,
            instrument=row.instrument,
            side=Side(row.side),
            quantity=row.quantity,
            price=row.price,
            gross_amount=row.gross_amount,
            transacted_at=naive_transacted_at,
            state=row.state,
            first_seen_file_id=source_file.id,
        )
        session.add(tx)
        session.flush()  # need tx.id for the version row

        version = TransactionVersion(
            transaction_id=tx.id,
            source_file_id=source_file.id,
            version_number=1,
            instrument=row.instrument,
            side=Side(row.side),
            quantity=row.quantity,
            price=row.price,
            gross_amount=row.gross_amount,
            transacted_at=naive_transacted_at,
            state=row.state,
        )
        session.add(version)
        session.flush()

        tx.current_version_id = version.id
        return "created"

    if not _row_differs_from_transaction(row, existing):
        return "unchanged"

    # Correction: close the current version, open a new one, update the
    # denormalized "current" fields on the Transaction itself.
    current_version = session.get(TransactionVersion, existing.current_version_id)
    next_version_number = (current_version.version_number + 1) if current_version else 1
    if current_version is not None:
        current_version.effective_to = source_file.imported_at

    new_version = TransactionVersion(
        transaction_id=existing.id,
        source_file_id=source_file.id,
        version_number=next_version_number,
        instrument=row.instrument,
        side=Side(row.side),
        quantity=row.quantity,
        price=row.price,
        gross_amount=row.gross_amount,
        transacted_at=naive_transacted_at,
        state=row.state,
        effective_from=source_file.imported_at,
    )
    session.add(new_version)
    session.flush()

    existing.instrument = row.instrument
    existing.side = Side(row.side)
    existing.quantity = row.quantity
    existing.price = row.price
    existing.gross_amount = row.gross_amount
    existing.transacted_at = naive_transacted_at
    existing.state = row.state
    existing.current_version_id = new_version.id

    return "corrected"
