"""
DB-aware orchestration around the pure matching engine in
``src/matching/core.py``.

This module's job is narrow: load current Transactions and ManualResolutions
out of the database, hand them to the pure engine, and persist whatever
comes back as a ReconciliationRun / ReconciliationResult / FieldDifference
tree. It contains no matching or comparison logic itself.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import AMOUNT_TOLERANCE, TIME_TOLERANCE_SECONDS
from src.matching.core import (
    ACCEPTED_AS_UNMATCHED,
    MATCHED_MANUALLY,
    ManualLink,
    MatchInput,
    run_matching,
)
from src.models import (
    FieldDifference,
    ManualResolution,
    MatchStatus,
    ReconciliationResult,
    ReconciliationRun,
    ResolutionType,
    SourceSystem,
    Transaction,
    TransactionState,
    utcnow,
)


def _to_match_input(tx: Transaction) -> MatchInput:
    return MatchInput(
        key=tx.natural_key,
        instrument=tx.instrument,
        side=tx.side.value,
        quantity=Decimal(tx.quantity),
        price=Decimal(tx.price),
        gross_amount=Decimal(tx.gross_amount),
        transacted_at=tx.transacted_at,
        state=tx.state,
        ref_id=tx.id,
    )


def _load_manual_links(session: Session) -> list[ManualLink]:
    resolutions = session.execute(select(ManualResolution)).scalars().all()
    links = []
    for r in resolutions:
        ledger_key = r.ledger_transaction.natural_key if r.ledger_transaction_id else None
        statement_key = r.statement_transaction.natural_key if r.statement_transaction_id else None
        resolution_type = MATCHED_MANUALLY if r.resolution_type == ResolutionType.MATCHED_MANUALLY \
            else ACCEPTED_AS_UNMATCHED
        links.append(ManualLink(resolution_type=resolution_type, ledger_key=ledger_key, statement_key=statement_key))
    return links


def run_reconciliation(session: Session, triggered_by: str = "manual") -> ReconciliationRun:
    """Run one full reconciliation pass and persist the results."""
    run = ReconciliationRun(started_at=utcnow(), status="RUNNING", triggered_by=triggered_by)
    session.add(run)
    session.flush()

    ledger_txs = session.execute(
        select(Transaction).where(Transaction.source_system == SourceSystem.LEDGER)
    ).scalars().all()
    statement_txs = session.execute(
        select(Transaction).where(Transaction.source_system == SourceSystem.STATEMENT)
    ).scalars().all()

    cancelled_excluded = sum(1 for t in ledger_txs + statement_txs if t.state == TransactionState.CANCELLED)

    ledger_inputs = [_to_match_input(t) for t in ledger_txs]
    statement_inputs = [_to_match_input(t) for t in statement_txs]
    manual_links = _load_manual_links(session)

    outcomes = run_matching(
        ledger_inputs,
        statement_inputs,
        manual_links=manual_links,
        amount_tolerance=AMOUNT_TOLERANCE,
        time_tolerance_seconds=TIME_TOLERANCE_SECONDS,
    )

    counts = {
        "MATCHED": 0,
        "MATCHED_WITHIN_TOLERANCE": 0,
        "DIFFERS": 0,
        "UNMATCHED_LEDGER": 0,
        "UNMATCHED_STATEMENT": 0,
        "MANUALLY_RESOLVED": 0,
    }
    for outcome in outcomes:
        counts[outcome.status] += 1
        result = ReconciliationResult(
            run_id=run.id,
            ledger_transaction_id=outcome.ledger.ref_id if outcome.ledger else None,
            statement_transaction_id=outcome.statement.ref_id if outcome.statement else None,
            match_status=MatchStatus(outcome.status),
        )
        session.add(result)
        session.flush()

        for diff in outcome.diffs:
            session.add(
                FieldDifference(
                    result_id=result.id,
                    field_name=diff.field_name,
                    ledger_value=diff.ledger_value,
                    statement_value=diff.statement_value,
                    delta=diff.delta,
                    is_significant=diff.is_significant,
                )
            )

    run.matched_count = counts["MATCHED"]
    run.matched_within_tolerance_count = counts["MATCHED_WITHIN_TOLERANCE"]
    run.differs_count = counts["DIFFERS"]
    run.unmatched_ledger_count = counts["UNMATCHED_LEDGER"]
    run.unmatched_statement_count = counts["UNMATCHED_STATEMENT"]
    run.manually_resolved_count = counts["MANUALLY_RESOLVED"]
    run.cancelled_excluded_count = cancelled_excluded
    run.status = "COMPLETED"
    run.finished_at = utcnow()

    return run
