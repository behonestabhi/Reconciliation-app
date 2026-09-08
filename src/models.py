"""
Data layer schema.

Design notes (see README.md for the full rationale):

- ``SourceFile`` records every file ever uploaded, keyed by a content hash,
  so re-sending the same file is detected and skipped rather than silently
  re-processed.
- ``Transaction`` holds the *current* view of a trade as seen by one of the
  two systems (``ledger`` or ``statement``). It is keyed on
  ``(system, natural_key)`` where natural_key is the trade_id / reference.
- ``TransactionVersion`` preserves every value a Transaction has ever held.
  When a correction file changes an amount, we do not overwrite history: we
  close the current version (set ``effective_to``) and open a new one. This
  is what lets someone ask "what did this row used to say?".
- ``ReconciliationRun`` / ``ReconciliationResult`` / ``FieldDifference``
  capture the output of a single morning run: which ledger row was paired
  with which statement row (or left unpaired), and exactly which fields
  disagreed and by how much.
- ``ManualResolution`` is deliberately independent of any single run. A
  human decision ("these two unmatched rows are actually the same trade",
  or "this row genuinely has no pair, stop flagging it") needs to survive
  into tomorrow's run, so it is keyed on the underlying Transactions, not on
  a ReconciliationResult that only exists for one run.
"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """All datetimes in this app are UTC, stored naive (SQLite has no
    timezone-aware datetime type). See README > Assumptions."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SourceSystem(str, enum.Enum):
    LEDGER = "LEDGER"
    STATEMENT = "STATEMENT"


class FileStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    PROCESSED_WITH_ERRORS = "PROCESSED_WITH_ERRORS"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


class TransactionState:
    """
    Canonical state strings this app recognizes and treats specially.
    Deliberately not a closed enum: a status this app doesn't recognize is
    stored verbatim (see ``normalize_state``) rather than collapsed into a
    generic bucket, so that two different unrecognized statuses are never
    mistaken for the same value during comparison. Only CANCELLED is ever
    branched on in code (to exclude a transaction from reconciliation);
    everything else is opaque text that a person can read and compare.
    """
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"
    PENDING = "PENDING"


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class MatchStatus(str, enum.Enum):
    MATCHED = "MATCHED"                                  # paired, every field identical
    MATCHED_WITHIN_TOLERANCE = "MATCHED_WITHIN_TOLERANCE"  # paired, differs only within tolerance
    DIFFERS = "DIFFERS"                                   # paired, but one or more fields disagree materially
    UNMATCHED_LEDGER = "UNMATCHED_LEDGER"   # ledger row with no statement counterpart
    UNMATCHED_STATEMENT = "UNMATCHED_STATEMENT"  # statement row with no ledger counterpart
    MANUALLY_RESOLVED = "MANUALLY_RESOLVED"  # a human decision now overrides the auto result


class ResolutionType(str, enum.Enum):
    MATCHED_MANUALLY = "MATCHED_MANUALLY"          # human paired two rows the system couldn't
    ACCEPTED_AS_UNMATCHED = "ACCEPTED_AS_UNMATCHED"  # human confirmed a row genuinely has no pair


# ---------------------------------------------------------------------------
# Import metadata
# ---------------------------------------------------------------------------

class SourceFile(Base):
    """
    One uploaded file. ``content_hash`` is a sha256 of the raw file bytes,
    unique across all imports, which is how we detect "the same file sent
    twice" regardless of filename.
    """
    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_system: Mapped[SourceSystem] = mapped_column(Enum(SourceSystem), nullable=False)
    # Nullable and unique: the real content hash is owned by exactly one row
    # (the original import). A file recognized as a duplicate is still
    # recorded (see FileStatus.DUPLICATE) but stores no hash of its own,
    # rather than inventing a suffixed value that would both violate the
    # column's width and misrepresent what this column means. Multiple NULLs
    # are legal under a UNIQUE constraint, so any number of duplicate
    # attempts can be recorded this way.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    status: Mapped[FileStatus] = mapped_column(Enum(FileStatus), nullable=False, default=FileStatus.PENDING)

    imported_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)

    row_count: Mapped[int] = mapped_column(Integer, default=0)
    rows_created: Mapped[int] = mapped_column(Integer, default=0)   # brand-new transactions
    rows_corrected: Mapped[int] = mapped_column(Integer, default=0)  # existing transactions whose values changed
    rows_unchanged: Mapped[int] = mapped_column(Integer, default=0)  # existing transactions re-sent identically
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)    # rows that failed validation

    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)  # True if any row_corrected > 0
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<SourceFile {self.filename} ({self.source_system.value}) {self.status.value}>"


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class Transaction(Base):
    """
    Current state of one trade as recorded by one system. Two rows with the
    same natural_key (e.g. "T-1001") in different systems are the two sides
    that reconciliation tries to pair up.
    """
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("source_system", "natural_key", name="uq_transaction_system_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_system: Mapped[SourceSystem] = mapped_column(Enum(SourceSystem), nullable=False)
    natural_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[Side] = mapped_column(Enum(Side), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    transacted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)  # canonical value or raw text, see TransactionState

    first_seen_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction_versions.id", use_alter=True, name="fk_current_version"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )

    versions: Mapped[list["TransactionVersion"]] = relationship(
        back_populates="transaction",
        foreign_keys="TransactionVersion.transaction_id",
        order_by="TransactionVersion.version_number",
    )

    def __repr__(self) -> str:
        return f"<Transaction {self.source_system.value}:{self.natural_key}>"


class TransactionVersion(Base):
    """
    A snapshot of a Transaction's values at one point in time. Version 1 is
    created on first import. A correction file that changes any monetary or
    timing field produces a new version and closes the previous one
    (``effective_to`` set) rather than overwriting it.
    """
    __tablename__ = "transaction_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False, index=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[Side] = mapped_column(Enum(Side), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    transacted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)  # canonical value or raw text, see TransactionState

    effective_from: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    transaction: Mapped["Transaction"] = relationship(
        back_populates="versions", foreign_keys=[transaction_id]
    )
    source_file: Mapped["SourceFile"] = relationship()

    __table_args__ = (
        UniqueConstraint("transaction_id", "version_number", name="uq_transaction_version"),
    )

    def __repr__(self) -> str:
        return f"<TransactionVersion tx={self.transaction_id} v{self.version_number}>"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationRun(Base):
    """One execution of the morning reconciliation process."""
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)

    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_within_tolerance_count: Mapped[int] = mapped_column(Integer, default=0)
    differs_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_ledger_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_statement_count: Mapped[int] = mapped_column(Integer, default=0)
    manually_resolved_count: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_excluded_count: Mapped[int] = mapped_column(Integer, default=0)

    results: Mapped[list["ReconciliationResult"]] = relationship(back_populates="run")

    def __repr__(self) -> str:
        return f"<ReconciliationRun {self.id} {self.status}>"


class ReconciliationResult(Base):
    """
    One outcome row from a run: a pairing (or lack of one) between a ledger
    Transaction and a statement Transaction. Cancelled transactions are
    excluded from the run entirely (see matching service), so they never
    appear here.
    """
    __tablename__ = "reconciliation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), nullable=False, index=True)

    ledger_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    statement_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    match_status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)

    run: Mapped["ReconciliationRun"] = relationship(back_populates="results")
    ledger_transaction: Mapped["Transaction | None"] = relationship(foreign_keys=[ledger_transaction_id])
    statement_transaction: Mapped["Transaction | None"] = relationship(foreign_keys=[statement_transaction_id])

    differences: Mapped[list["FieldDifference"]] = relationship(back_populates="result")

    def __repr__(self) -> str:
        return f"<ReconciliationResult {self.id} {self.match_status.value}>"


class FieldDifference(Base):
    """One field that disagreed between a matched pair, and by how much."""
    __tablename__ = "field_differences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_results.id"), nullable=False, index=True)

    field_name: Mapped[str] = mapped_column(String(32), nullable=False)
    ledger_value: Mapped[str] = mapped_column(String(64), nullable=False)
    statement_value: Mapped[str] = mapped_column(String(64), nullable=False)
    delta: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)  # numeric fields only
    is_significant: Mapped[bool] = mapped_column(Boolean, nullable=False)

    result: Mapped["ReconciliationResult"] = relationship(back_populates="differences")

    def __repr__(self) -> str:
        return f"<FieldDifference {self.field_name}: {self.ledger_value} vs {self.statement_value}>"


class ManualResolution(Base):
    """
    A human decision that must hold in future runs. Keyed on the underlying
    Transactions (not a specific run's ReconciliationResult) so that it is
    still honoured tomorrow, even though tomorrow's run creates entirely new
    ReconciliationResult rows.
    """
    __tablename__ = "manual_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resolution_type: Mapped[ResolutionType] = mapped_column(Enum(ResolutionType), nullable=False)

    # unique=True (with nullable=True) is a DB-level backstop for the "one
    # manual decision per transaction" rule that resolution_service already
    # checks at the application level: multiple NULLs are legal under a
    # UNIQUE constraint, but two rows can never claim the same real
    # transaction id, even under concurrent requests racing past the
    # application-level check.
    ledger_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, unique=True
    )
    statement_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, unique=True
    )

    resolved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    ledger_transaction: Mapped["Transaction | None"] = relationship(foreign_keys=[ledger_transaction_id])
    statement_transaction: Mapped["Transaction | None"] = relationship(foreign_keys=[statement_transaction_id])

    def __repr__(self) -> str:
        return f"<ManualResolution {self.resolution_type.value}>"
