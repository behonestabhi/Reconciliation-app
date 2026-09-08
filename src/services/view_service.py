"""
Read-model queries for the UI.

Nothing here computes a match or a difference -- that logic lives entirely
in ``src/matching/core.py`` and is only ever run by
``reconciliation_service.run_reconciliation``. This module's job is to
assemble what's already been persisted (runs, results, field differences,
transaction versions, manual resolutions) into plain dicts a template can
render, and to apply filtering/sorting for the results page. Keeping this
separate is what lets ``src/web/routes.py`` stay thin: routes call these
functions, they never touch a model directly to compute anything.
"""
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.config import AMOUNT_TOLERANCE, TIME_TOLERANCE_SECONDS
from src.models import (
    FieldDifference,
    ManualResolution,
    MatchStatus,
    ReconciliationResult,
    ReconciliationRun,
    SourceSystem,
    Transaction,
)

# Order results are shown in by default: the things a person needs to look
# at first (real problems, unresolved rows) before the things that are fine.
STATUS_PRIORITY = {
    MatchStatus.DIFFERS: 0,
    MatchStatus.UNMATCHED_LEDGER: 1,
    MatchStatus.UNMATCHED_STATEMENT: 1,
    MatchStatus.MATCHED_WITHIN_TOLERANCE: 2,
    MatchStatus.MANUALLY_RESOLVED: 3,
    MatchStatus.MATCHED: 4,
}

STATUS_LABELS = {
    MatchStatus.MATCHED: "Matched",
    MatchStatus.MATCHED_WITHIN_TOLERANCE: "Matched (within tolerance)",
    MatchStatus.DIFFERS: "Differs",
    MatchStatus.UNMATCHED_LEDGER: "Unmatched \u2014 ours only",
    MatchStatus.UNMATCHED_STATEMENT: "Unmatched \u2014 counterparty only",
    MatchStatus.MANUALLY_RESOLVED: "Manually resolved",
}

FIELD_TOLERANCES = {
    "quantity": "exact match required",
    "price": f"\u00b1{AMOUNT_TOLERANCE}",
    "gross_amount": f"\u00b1{AMOUNT_TOLERANCE}",
    "transacted_at": f"\u00b1{TIME_TOLERANCE_SECONDS}s",
    "instrument": "exact match required",
    "side": "exact match required",
    "state": "exact match required",
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def list_runs(session: Session) -> list[dict]:
    runs = session.execute(
        select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc())
    ).scalars().all()
    return [summarize_run(r) for r in runs]


def get_latest_run_summary(session: Session) -> dict | None:
    run = session.execute(
        select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    return summarize_run(run) if run else None


def get_import_counts(session: Session) -> dict:
    ledger_count = session.execute(
        select(Transaction).where(Transaction.source_system == SourceSystem.LEDGER)
    ).scalars().all()
    statement_count = session.execute(
        select(Transaction).where(Transaction.source_system == SourceSystem.STATEMENT)
    ).scalars().all()
    return {"ledger_transactions": len(ledger_count), "statement_transactions": len(statement_count)}


def summarize_run(run: ReconciliationRun) -> dict:
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "triggered_by": run.triggered_by,
        "matched_count": run.matched_count,
        "matched_within_tolerance_count": run.matched_within_tolerance_count,
        "differs_count": run.differs_count,
        "unmatched_ledger_count": run.unmatched_ledger_count,
        "unmatched_statement_count": run.unmatched_statement_count,
        "manually_resolved_count": run.manually_resolved_count,
        "cancelled_excluded_count": run.cancelled_excluded_count,
        "total_results": (
            run.matched_count + run.matched_within_tolerance_count + run.differs_count
            + run.unmatched_ledger_count + run.unmatched_statement_count + run.manually_resolved_count
        ),
        "needs_attention": run.differs_count + run.unmatched_ledger_count + run.unmatched_statement_count,
    }


# ---------------------------------------------------------------------------
# Run results (the "morning screen")
# ---------------------------------------------------------------------------

def get_run(session: Session, run_id: int) -> ReconciliationRun | None:
    return session.get(ReconciliationRun, run_id)


def list_results(
    session: Session,
    run_id: int,
    status_filter: str | None = None,
    sort: str = "status",
) -> list[dict]:
    query = select(ReconciliationResult).where(ReconciliationResult.run_id == run_id)
    if status_filter:
        query = query.where(ReconciliationResult.match_status == MatchStatus(status_filter))
    results = session.execute(query).scalars().all()

    rows = [_result_row(r) for r in results]

    if sort == "natural_key":
        rows.sort(key=lambda r: r["natural_key"] or "")
    elif sort == "delta":
        rows.sort(key=lambda r: (r["max_delta"] is None, -(r["max_delta"] or 0)))
    else:  # "status" (default)
        rows.sort(key=lambda r: (STATUS_PRIORITY[MatchStatus(r["status"])], r["natural_key"] or ""))

    return rows


def _result_row(result: ReconciliationResult) -> dict:
    ledger_tx = result.ledger_transaction
    statement_tx = result.statement_transaction
    natural_key = (ledger_tx.natural_key if ledger_tx else None) or (statement_tx.natural_key if statement_tx else None)
    instrument = (ledger_tx.instrument if ledger_tx else None) or (statement_tx.instrument if statement_tx else None)

    significant_diffs = [d for d in result.differences if d.is_significant]
    # Only significant diffs count toward "largest difference" sorting -
    # mixing in tolerable drift would let a harmless few-second clock drift
    # outrank a real, material dollar discrepancy just because seconds and
    # currency units happen to be different magnitudes.
    numeric_deltas = [float(d.delta) for d in significant_diffs if d.delta is not None]

    return {
        "id": result.id,
        "status": result.match_status.value,
        "status_label": STATUS_LABELS[result.match_status],
        "natural_key": natural_key,
        "instrument": instrument,
        "ledger_transaction_id": ledger_tx.id if ledger_tx else None,
        "statement_transaction_id": statement_tx.id if statement_tx else None,
        "significant_diff_count": len(significant_diffs),
        "diff_fields": ", ".join(d.field_name for d in significant_diffs) if significant_diffs else "",
        "max_delta": max(numeric_deltas) if numeric_deltas else None,
    }


# ---------------------------------------------------------------------------
# Result / transaction detail
# ---------------------------------------------------------------------------

def _tx_detail(tx: Transaction | None) -> dict | None:
    if tx is None:
        return None
    return {
        "id": tx.id,
        "natural_key": tx.natural_key,
        "instrument": tx.instrument,
        "side": tx.side.value,
        "quantity": str(Decimal(tx.quantity)),
        "price": str(Decimal(tx.price)),
        "gross_amount": str(Decimal(tx.gross_amount)),
        "transacted_at": tx.transacted_at,
        "state": tx.state,
        "created_at": tx.created_at,
        "updated_at": tx.updated_at,
    }


def _version_history(tx: Transaction | None) -> list[dict]:
    if tx is None:
        return []
    return [
        {
            "version_number": v.version_number,
            "quantity": str(Decimal(v.quantity)),
            "price": str(Decimal(v.price)),
            "gross_amount": str(Decimal(v.gross_amount)),
            "transacted_at": v.transacted_at,
            "state": v.state,
            "source_filename": v.source_file.filename,
            "effective_from": v.effective_from,
            "effective_to": v.effective_to,
            "is_current": v.effective_to is None,
        }
        for v in tx.versions
    ]


def _manual_resolution_detail(session: Session, ledger_tx_id: int | None, statement_tx_id: int | None) -> dict | None:
    conditions = []
    if ledger_tx_id is not None:
        conditions.append(ManualResolution.ledger_transaction_id == ledger_tx_id)
    if statement_tx_id is not None:
        conditions.append(ManualResolution.statement_transaction_id == statement_tx_id)
    if not conditions:
        return None

    resolution = session.execute(select(ManualResolution).where(or_(*conditions))).scalars().first()
    if resolution is None:
        return None
    return {
        "resolution_type": resolution.resolution_type.value,
        "resolved_by": resolution.resolved_by,
        "resolved_at": resolution.resolved_at,
        "notes": resolution.notes,
    }


def list_unmatched_candidates(session: Session, run_id: int, want_system: SourceSystem) -> list[dict]:
    """
    Candidates for "match this with...": the other side's still-unmatched
    results in the same run.
    """
    status = MatchStatus.UNMATCHED_LEDGER if want_system == SourceSystem.LEDGER else MatchStatus.UNMATCHED_STATEMENT
    results = session.execute(
        select(ReconciliationResult).where(
            ReconciliationResult.run_id == run_id,
            ReconciliationResult.match_status == status,
        )
    ).scalars().all()

    candidates = []
    for r in results:
        tx = r.ledger_transaction if want_system == SourceSystem.LEDGER else r.statement_transaction
        if tx is None:
            continue
        candidates.append({
            "result_id": r.id,
            "transaction_id": tx.id,
            "natural_key": tx.natural_key,
            "instrument": tx.instrument,
            "side": tx.side.value,
            "gross_amount": str(Decimal(tx.gross_amount)),
            "transacted_at": tx.transacted_at,
        })
    candidates.sort(key=lambda c: c["natural_key"])
    return candidates


def get_result_detail(session: Session, result_id: int) -> dict | None:
    result = session.get(ReconciliationResult, result_id)
    if result is None:
        return None

    ledger_tx = result.ledger_transaction
    statement_tx = result.statement_transaction

    diffs = [
        {
            "field_name": d.field_name,
            "ledger_value": d.ledger_value,
            "statement_value": d.statement_value,
            "delta": str(Decimal(d.delta)) if d.delta is not None else None,
            "is_significant": d.is_significant,
            "tolerance": FIELD_TOLERANCES.get(d.field_name, ""),
        }
        for d in result.differences
    ]

    manual_resolution = None
    if result.match_status == MatchStatus.MANUALLY_RESOLVED:
        manual_resolution = _manual_resolution_detail(
            session,
            ledger_tx.id if ledger_tx else None,
            statement_tx.id if statement_tx else None,
        )

    candidates = []
    if result.match_status == MatchStatus.UNMATCHED_LEDGER:
        candidates = list_unmatched_candidates(session, result.run_id, SourceSystem.STATEMENT)
    elif result.match_status == MatchStatus.UNMATCHED_STATEMENT:
        candidates = list_unmatched_candidates(session, result.run_id, SourceSystem.LEDGER)

    return {
        "id": result.id,
        "run_id": result.run_id,
        "status": result.match_status.value,
        "status_label": STATUS_LABELS[result.match_status],
        "ledger": _tx_detail(ledger_tx),
        "statement": _tx_detail(statement_tx),
        "ledger_versions": _version_history(ledger_tx),
        "statement_versions": _version_history(statement_tx),
        "diffs": diffs,
        "manual_resolution": manual_resolution,
        "candidates": candidates,
        "can_resolve": result.match_status in (MatchStatus.UNMATCHED_LEDGER, MatchStatus.UNMATCHED_STATEMENT),
    }
