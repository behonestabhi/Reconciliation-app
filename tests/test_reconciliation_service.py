from decimal import Decimal

from sqlalchemy import select

from src.models import (
    MatchStatus,
    ReconciliationResult,
    ReconciliationRun,
    Transaction,
)
from src.services.import_service import import_file
from src.services.reconciliation_service import run_reconciliation
from tests.conftest import read_sample_bytes


def _import_normal_day(session):
    import_file(session, "ledger_2025-07.csv", read_sample_bytes("ledger_2025-07.csv"), source_system="LEDGER")
    import_file(session, "statement_2025-07.csv", read_sample_bytes("statement_2025-07.csv"),
                source_system="STATEMENT")


class TestRunReconciliation:
    def test_produces_expected_statuses_for_sample_data(self, db_session):
        _import_normal_day(db_session)
        run = run_reconciliation(db_session, triggered_by="test")

        assert run.status == "COMPLETED"
        assert run.finished_at is not None

        # T-1001 exact match
        assert run.matched_count >= 1
        # T-1002 (rounding), T-1003 (clock drift) - within tolerance
        assert run.matched_within_tolerance_count == 2
        # T-1005 (price), T-1006 (timing) - real problems
        assert run.differs_count == 2
        # T-1004, T-1010 ledger-only
        assert run.unmatched_ledger_count == 2
        # C-9001 statement-only
        assert run.unmatched_statement_count == 1
        # T-1007 cancelled on both sides - excluded, not counted anywhere
        assert run.cancelled_excluded_count == 2

    def test_cancelled_transactions_produce_no_result_row(self, db_session):
        _import_normal_day(db_session)
        run = run_reconciliation(db_session)
        results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)
        ).scalars().all()

        for result in results:
            ledger_tx = result.ledger_transaction
            statement_tx = result.statement_transaction
            if ledger_tx:
                assert ledger_tx.natural_key != "T-1007"
            if statement_tx:
                assert statement_tx.natural_key != "T-1007"

    def test_field_differences_recorded_for_a_differs_result(self, db_session):
        _import_normal_day(db_session)
        run = run_reconciliation(db_session)
        results = db_session.execute(
            select(ReconciliationResult).where(
                ReconciliationResult.run_id == run.id,
                ReconciliationResult.match_status == MatchStatus.DIFFERS,
            )
        ).scalars().all()

        t1005_result = next(r for r in results if r.ledger_transaction.natural_key == "T-1005")
        field_names = {d.field_name for d in t1005_result.differences}
        assert "price" in field_names
        assert "gross_amount" in field_names
        price_diff = next(d for d in t1005_result.differences if d.field_name == "price")
        assert price_diff.is_significant is True
        assert Decimal(price_diff.delta) == Decimal("50")

    def test_uses_current_corrected_values_not_original(self, db_session):
        # Import base files, then a ledger correction that fixes T-1008's
        # quantity. Reconciliation must use the corrected value.
        import_file(db_session, "ledger_2025-07.csv", read_sample_bytes("ledger_2025-07.csv"),
                    source_system="LEDGER")
        import_file(db_session, "statement_2025-07.csv", read_sample_bytes("statement_2025-07.csv"),
                    source_system="STATEMENT")
        import_file(db_session, "ledger_2025-07_correction.csv",
                    read_sample_bytes("ledger_2025-07_correction.csv"), source_system="LEDGER")

        run = run_reconciliation(db_session)
        results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)
        ).scalars().all()
        t1008_result = next(
            r for r in results
            if r.ledger_transaction and r.ledger_transaction.natural_key == "T-1008"
        )
        # Ledger corrected to 0.06/3870.00, statement still says 0.05/3225.00
        # -> now a real, significant discrepancy where before correction it
        # matched exactly.
        assert t1008_result.match_status == MatchStatus.DIFFERS
        field_names = {d.field_name for d in t1008_result.differences}
        assert "quantity" in field_names
        assert "gross_amount" in field_names

    def test_two_runs_both_persist_independently(self, db_session):
        _import_normal_day(db_session)
        run1 = run_reconciliation(db_session, triggered_by="first")
        run2 = run_reconciliation(db_session, triggered_by="second")

        assert run1.id != run2.id
        all_runs = db_session.execute(select(ReconciliationRun)).scalars().all()
        assert len(all_runs) == 2

        run1_results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run1.id)
        ).scalars().all()
        run2_results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run2.id)
        ).scalars().all()
        # Nothing changed between runs, so both produce the same number of
        # results, but they are distinct rows (history is preserved, not
        # overwritten).
        assert len(run1_results) == len(run2_results)
        assert {r.id for r in run1_results}.isdisjoint({r.id for r in run2_results})
