"""
Manual reconciliation decisions: a human pairing two transactions the
system couldn't auto-match, or confirming that an unmatched transaction
genuinely has no counterpart.

These are persisted independently of any single ReconciliationRun (see
``models.ManualResolution``) precisely so they hold in future runs -- the
matching engine reloads them fresh every time it runs (see
``reconciliation_service._load_manual_links``).

This module owns the validation that keeps that persisted state sane:
  - cancelled transactions can never be reconciled, manually or otherwise
  - a transaction already covered by one manual decision cannot be pulled
    into a second, conflicting one (no double/ambiguous matching)
  - a manual match must pair one ledger transaction with one statement
    transaction, not two of the same side
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import ManualResolution, ResolutionType, SourceSystem, Transaction, TransactionState


class ManualResolutionError(ValueError):
    """Raised when a requested manual resolution is invalid or ambiguous."""


def _get_transaction(session: Session, transaction_id: int) -> Transaction:
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        raise ManualResolutionError(f"transaction {transaction_id} does not exist")
    return tx


def _ensure_not_cancelled(tx: Transaction) -> None:
    if tx.state == TransactionState.CANCELLED:
        raise ManualResolutionError(
            f"transaction {tx.id} ({tx.natural_key}) is CANCELLED and cannot be reconciled"
        )


def _ensure_not_already_resolved(session: Session, transaction_id: int) -> None:
    existing = session.execute(
        select(ManualResolution).where(
            (ManualResolution.ledger_transaction_id == transaction_id)
            | (ManualResolution.statement_transaction_id == transaction_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ManualResolutionError(
            f"transaction {transaction_id} is already covered by manual resolution {existing.id} "
            f"({existing.resolution_type.value}); undo that first rather than creating a conflicting one"
        )


def _add_and_flush(session: Session, resolution: ManualResolution) -> ManualResolution:
    """
    The application-level ``_ensure_not_already_resolved`` check above has a
    narrow race window (two requests both checking before either commits);
    the model's UNIQUE constraint on each transaction id column is the
    database-level backstop. If that backstop is what actually catches a
    conflict, surface it the same way as the application-level check rather
    than letting a raw IntegrityError become an unhandled 500.
    """
    session.add(resolution)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ManualResolutionError(
            "one of these transactions was just resolved by someone else - refresh and try again"
        ) from exc
    return resolution


def create_manual_match(
    session: Session,
    ledger_transaction_id: int,
    statement_transaction_id: int,
    resolved_by: str,
    notes: str | None = None,
) -> ManualResolution:
    """Pair a ledger transaction with a statement transaction by hand."""
    ledger_tx = _get_transaction(session, ledger_transaction_id)
    statement_tx = _get_transaction(session, statement_transaction_id)

    if ledger_tx.source_system != SourceSystem.LEDGER:
        raise ManualResolutionError(
            f"transaction {ledger_transaction_id} is not a LEDGER transaction "
            f"(it's {ledger_tx.source_system.value})"
        )
    if statement_tx.source_system != SourceSystem.STATEMENT:
        raise ManualResolutionError(
            f"transaction {statement_transaction_id} is not a STATEMENT transaction "
            f"(it's {statement_tx.source_system.value})"
        )

    _ensure_not_cancelled(ledger_tx)
    _ensure_not_cancelled(statement_tx)
    _ensure_not_already_resolved(session, ledger_transaction_id)
    _ensure_not_already_resolved(session, statement_transaction_id)

    resolution = ManualResolution(
        resolution_type=ResolutionType.MATCHED_MANUALLY,
        ledger_transaction_id=ledger_tx.id,
        statement_transaction_id=statement_tx.id,
        resolved_by=resolved_by,
        notes=notes,
    )
    return _add_and_flush(session, resolution)


def create_manual_accept_unmatched(
    session: Session,
    transaction_id: int,
    resolved_by: str,
    notes: str | None = None,
) -> ManualResolution:
    """Confirm that a transaction genuinely has no counterpart, on either side."""
    tx = _get_transaction(session, transaction_id)
    _ensure_not_cancelled(tx)
    _ensure_not_already_resolved(session, transaction_id)

    resolution = ManualResolution(
        resolution_type=ResolutionType.ACCEPTED_AS_UNMATCHED,
        ledger_transaction_id=tx.id if tx.source_system == SourceSystem.LEDGER else None,
        statement_transaction_id=tx.id if tx.source_system == SourceSystem.STATEMENT else None,
        resolved_by=resolved_by,
        notes=notes,
    )
    return _add_and_flush(session, resolution)
