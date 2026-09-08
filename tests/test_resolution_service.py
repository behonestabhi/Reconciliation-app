import pytest
from sqlalchemy import select

from src.models import MatchStatus, ReconciliationResult, SourceSystem, Transaction
from src.services.import_service import import_file
from src.services.reconciliation_service import run_reconciliation
from src.services.resolution_service import (
    ManualResolutionError,
    create_manual_accept_unmatched,
    create_manual_match,
)
from tests.conftest import read_sample_bytes


def _import_normal_day(session):
    import_file(session, "ledger_2025-07.csv", read_sample_bytes("ledger_2025-07.csv"), source_system="LEDGER")
    import_file(session, "statement_2025-07.csv", read_sample_bytes("statement_2025-07.csv"),
                source_system="STATEMENT")


def _tx(session, system: SourceSystem, natural_key: str) -> Transaction:
    return session.execute(
        select(Transaction).where(Transaction.source_system == system, Transaction.natural_key == natural_key)
    ).scalar_one()


class TestManualMatch:
    def test_pairs_two_unmatched_transactions(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")

        resolution = create_manual_match(db_session, ledger_tx.id, statement_tx.id, resolved_by="alice",
                                          notes="confirmed same trade over the phone")
        assert resolution.ledger_transaction_id == ledger_tx.id
        assert resolution.statement_transaction_id == statement_tx.id
        assert resolution.notes == "confirmed same trade over the phone"

    def test_rejects_wrong_side_arguments(self, db_session):
        _import_normal_day(db_session)
        two_ledger_txs = (
            _tx(db_session, SourceSystem.LEDGER, "T-1004"),
            _tx(db_session, SourceSystem.LEDGER, "T-1010"),
        )
        with pytest.raises(ManualResolutionError):
            create_manual_match(db_session, two_ledger_txs[0].id, two_ledger_txs[1].id, resolved_by="alice")

    def test_rejects_cancelled_ledger_transaction(self, db_session):
        _import_normal_day(db_session)
        cancelled_tx = _tx(db_session, SourceSystem.LEDGER, "T-1007")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        with pytest.raises(ManualResolutionError, match="CANCELLED"):
            create_manual_match(db_session, cancelled_tx.id, statement_tx.id, resolved_by="alice")

    def test_rejects_cancelled_statement_transaction(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        cancelled_tx = _tx(db_session, SourceSystem.STATEMENT, "T-1007")
        with pytest.raises(ManualResolutionError, match="CANCELLED"):
            create_manual_match(db_session, ledger_tx.id, cancelled_tx.id, resolved_by="alice")

    def test_prevents_matching_a_transaction_twice(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        create_manual_match(db_session, ledger_tx.id, statement_tx.id, resolved_by="alice")

        # Try to reuse the same ledger transaction against a *different*
        # statement transaction.
        another_statement_tx = _tx(db_session, SourceSystem.STATEMENT, "T-1009")
        with pytest.raises(ManualResolutionError, match="already covered"):
            create_manual_match(db_session, ledger_tx.id, another_statement_tx.id, resolved_by="bob")

    def test_rejects_nonexistent_transaction(self, db_session):
        _import_normal_day(db_session)
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        with pytest.raises(ManualResolutionError):
            create_manual_match(db_session, 999999, statement_tx.id, resolved_by="alice")

    def test_db_level_constraint_backstops_the_application_check(self, db_session):
        # Regression test: the application-level "already resolved" check
        # has a narrow TOCTOU race window between two concurrent requests.
        # This simulates that race by inserting a conflicting row directly,
        # bypassing the application check, to prove the database's own
        # UNIQUE constraint (and this service's handling of the resulting
        # IntegrityError) is the real backstop, not just the Python check.
        from src.models import ManualResolution, ResolutionType

        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")

        db_session.add(ManualResolution(
            resolution_type=ResolutionType.ACCEPTED_AS_UNMATCHED,
            ledger_transaction_id=ledger_tx.id,
            resolved_by="racer",
        ))
        db_session.flush()

        # Bypass the ORM-level duplicate check that would normally catch
        # this, to prove the DB constraint itself is what stops it.
        with pytest.raises(ManualResolutionError, match="resolved by someone else"):
            resolution = ManualResolution(
                resolution_type=ResolutionType.MATCHED_MANUALLY,
                ledger_transaction_id=ledger_tx.id,
                statement_transaction_id=statement_tx.id,
                resolved_by="alice",
            )
            from src.services.resolution_service import _add_and_flush
            _add_and_flush(db_session, resolution)


class TestManualAcceptUnmatched:
    def test_accepts_a_genuinely_unmatched_ledger_row(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        resolution = create_manual_accept_unmatched(db_session, ledger_tx.id, resolved_by="alice",
                                                      notes="internal transfer, no counterparty")
        assert resolution.ledger_transaction_id == ledger_tx.id
        assert resolution.statement_transaction_id is None

    def test_rejects_cancelled_transaction(self, db_session):
        _import_normal_day(db_session)
        cancelled_tx = _tx(db_session, SourceSystem.LEDGER, "T-1007")
        with pytest.raises(ManualResolutionError, match="CANCELLED"):
            create_manual_accept_unmatched(db_session, cancelled_tx.id, resolved_by="alice")

    def test_prevents_double_resolution(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        create_manual_accept_unmatched(db_session, ledger_tx.id, resolved_by="alice")
        with pytest.raises(ManualResolutionError, match="already covered"):
            create_manual_accept_unmatched(db_session, ledger_tx.id, resolved_by="bob")

    def test_cannot_accept_unmatched_then_also_manually_match(self, db_session):
        _import_normal_day(db_session)
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        create_manual_accept_unmatched(db_session, ledger_tx.id, resolved_by="alice")
        with pytest.raises(ManualResolutionError, match="already covered"):
            create_manual_match(db_session, ledger_tx.id, statement_tx.id, resolved_by="bob")


class TestManualResolutionPersistsAcrossRuns:
    def test_manual_match_holds_in_a_second_run(self, db_session):
        _import_normal_day(db_session)
        run_reconciliation(db_session, triggered_by="day-1")  # T-1004/C-9001 both unmatched here

        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        create_manual_match(db_session, ledger_tx.id, statement_tx.id, resolved_by="alice")

        run2 = run_reconciliation(db_session, triggered_by="day-2")
        results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run2.id)
        ).scalars().all()

        manual_result = next(
            r for r in results
            if r.ledger_transaction_id == ledger_tx.id and r.statement_transaction_id == statement_tx.id
        )
        assert manual_result.match_status == MatchStatus.MANUALLY_RESOLVED
        # And it no longer shows up as unmatched anywhere in the new run.
        assert not any(
            r.match_status == MatchStatus.UNMATCHED_LEDGER and r.ledger_transaction_id == ledger_tx.id
            for r in results
        )
        assert not any(
            r.match_status == MatchStatus.UNMATCHED_STATEMENT and r.statement_transaction_id == statement_tx.id
            for r in results
        )

    def test_accepted_unmatched_holds_in_a_second_run(self, db_session):
        _import_normal_day(db_session)
        run_reconciliation(db_session, triggered_by="day-1")

        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        create_manual_accept_unmatched(db_session, ledger_tx.id, resolved_by="alice",
                                        notes="known internal-only trade")

        run2 = run_reconciliation(db_session, triggered_by="day-2")
        results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run2.id)
        ).scalars().all()
        result = next(r for r in results if r.ledger_transaction_id == ledger_tx.id)
        assert result.match_status == MatchStatus.MANUALLY_RESOLVED

    def test_resolution_survives_a_third_run_unchanged(self, db_session):
        # Not a one-time effect: it should hold indefinitely, not just for
        # the next run.
        _import_normal_day(db_session)
        run_reconciliation(db_session, triggered_by="day-1")
        ledger_tx = _tx(db_session, SourceSystem.LEDGER, "T-1004")
        statement_tx = _tx(db_session, SourceSystem.STATEMENT, "C-9001")
        create_manual_match(db_session, ledger_tx.id, statement_tx.id, resolved_by="alice")

        run_reconciliation(db_session, triggered_by="day-2")
        run3 = run_reconciliation(db_session, triggered_by="day-3")

        results = db_session.execute(
            select(ReconciliationResult).where(ReconciliationResult.run_id == run3.id)
        ).scalars().all()
        result = next(
            r for r in results
            if r.ledger_transaction_id == ledger_tx.id and r.statement_transaction_id == statement_tx.id
        )
        assert result.match_status == MatchStatus.MANUALLY_RESOLVED
