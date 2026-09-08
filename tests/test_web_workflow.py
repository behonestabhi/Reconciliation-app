"""
End-to-end tests driving the actual Flask app through its test client,
against a real (temp file) SQLite database -- not the in-memory fixture
used by the service-layer tests. This exercises the full stack: routes,
templates, services, and the DB together, covering the workflow described
in the assignment: import -> run -> inspect -> resolve -> run again ->
verify the resolution persisted.
"""
import importlib
import os

import pytest

from tests.conftest import sample_path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "recon_test.db"
    monkeypatch.setenv("RECON_DATABASE_URL", f"sqlite:///{db_path}")

    # src.config/src.db read the env var at import time, and app.py imports
    # them at module load, so force a clean reimport of the whole stack for
    # this test's isolated database.
    for mod_name in list(importlib.sys.modules):
        if mod_name == "src" or mod_name.startswith("src.") or mod_name == "app":
            del importlib.sys.modules[mod_name]

    import app as app_module
    from src.models import Base
    from src.db import engine

    Base.metadata.create_all(engine)

    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def _upload(client, filename, source_system=None):
    with open(sample_path(filename), "rb") as f:
        return client.post(
            "/imports",
            data={"file": (f, filename), "source_system": source_system or ""},
            content_type="multipart/form-data",
            follow_redirects=True,
        )


class TestApplicationStartup:
    def test_dashboard_loads_with_no_data(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"No reconciliation runs yet" in resp.data

    def test_imports_page_loads(self, client):
        resp = client.get("/imports")
        assert resp.status_code == 200


class TestImportWorkflow:
    def test_upload_ledger_and_statement(self, client):
        r1 = _upload(client, "ledger_2025-07.csv", "LEDGER")
        assert r1.status_code == 200
        assert b"PROCESSED" in r1.data

        r2 = _upload(client, "statement_2025-07.csv")  # auto-detect
        assert r2.status_code == 200
        assert b"PROCESSED" in r2.data

    def test_duplicate_upload_is_flagged(self, client):
        _upload(client, "ledger_2025-07.csv", "LEDGER")
        resp = _upload(client, "ledger_2025-07.csv", "LEDGER")
        assert b"DUPLICATE" in resp.data

    def test_uploading_with_no_file_shows_error(self, client):
        resp = client.post("/imports", data={}, content_type="multipart/form-data", follow_redirects=True)
        assert b"choose a file" in resp.data


class TestReconciliationWorkflow:
    def _seed(self, client):
        _upload(client, "ledger_2025-07.csv", "LEDGER")
        _upload(client, "statement_2025-07.csv", "STATEMENT")

    def test_start_run_from_dashboard(self, client):
        self._seed(client)
        resp = client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Run #1" in resp.data or b"Run &#35;1" in resp.data or b"Run" in resp.data

    def test_dashboard_shows_latest_run_counts(self, client):
        self._seed(client)
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Latest run" in resp.data

    def test_run_detail_lists_results(self, client):
        self._seed(client)
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        resp = client.get("/runs/1")
        assert resp.status_code == 200
        assert b"T-1001" in resp.data  # exact match should be listed
        assert b"C-9001" in resp.data  # unmatched statement-only row

    def test_run_detail_filter_by_status(self, client):
        self._seed(client)
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        resp = client.get("/runs/1?status=DIFFERS")
        assert resp.status_code == 200
        assert b"T-1005" in resp.data  # the real price discrepancy
        assert b"T-1001" not in resp.data  # exact match shouldn't appear under DIFFERS

    def test_run_detail_unknown_run_redirects_to_dashboard(self, client):
        resp = client.get("/runs/999", follow_redirects=True)
        assert resp.status_code == 200
        assert b"does not exist" in resp.data

    def test_result_detail_shows_both_sides(self, client):
        self._seed(client)
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        resp = client.get("/runs/1")
        # Find a result id from the DIFFERS filter (T-1005) to inspect.
        resp2 = client.get("/runs/1?status=DIFFERS")
        assert b"T-1005" in resp2.data

    def test_result_detail_page_for_unmatched_offers_resolution_form(self, client):
        self._seed(client)
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)
        # T-1004 is ledger-only/unmatched; find its result id via the DB directly
        # since we need the numeric result id to hit /results/<id>.
        from src.db import session_scope
        from src.services import view_service

        with session_scope() as session:
            results = view_service.list_results(session, 1, status_filter="UNMATCHED_LEDGER")
            t1004_result_id = next(r["id"] for r in results if r["natural_key"] == "T-1004")

        resp = client.get(f"/results/{t1004_result_id}")
        assert resp.status_code == 200
        assert b"Resolve this manually" in resp.data
        assert b"Accept as genuinely unmatched" in resp.data


class TestManualResolutionEndToEnd:
    def _seed_and_run(self, client):
        _upload(client, "ledger_2025-07.csv", "LEDGER")
        _upload(client, "statement_2025-07.csv", "STATEMENT")
        client.post("/runs", data={"triggered_by": "alice"}, follow_redirects=True)

    def _get_result_id(self, natural_key, status_filter):
        from src.db import session_scope
        from src.services import view_service

        with session_scope() as session:
            results = view_service.list_results(session, 1, status_filter=status_filter)
            return next(r["id"] for r in results if r["natural_key"] == natural_key)

    def test_manual_match_reflected_in_new_run(self, client):
        self._seed_and_run(client)
        ledger_result_id = self._get_result_id("T-1004", "UNMATCHED_LEDGER")

        detail_resp = client.get(f"/results/{ledger_result_id}")
        assert b"C-9001" in detail_resp.data  # the candidate should be offered

        from src.db import session_scope
        from src.models import SourceSystem, Transaction
        from sqlalchemy import select

        with session_scope() as session:
            statement_tx = session.execute(
                select(Transaction).where(
                    Transaction.source_system == SourceSystem.STATEMENT,
                    Transaction.natural_key == "C-9001",
                )
            ).scalar_one()
            statement_tx_id = statement_tx.id

        resolve_resp = client.post(
            f"/results/{ledger_result_id}/resolve",
            data={
                "action": "match_with",
                "resolved_by": "alice",
                "notes": "confirmed by phone",
                "counterpart_transaction_id": str(statement_tx_id),
            },
            follow_redirects=True,
        )
        assert resolve_resp.status_code == 200
        assert b"resolution saved" in resolve_resp.data

        # A new run (#2) should now show this pair as MANUALLY_RESOLVED.
        run2_resp = client.get("/runs/2?status=MANUALLY_RESOLVED")
        assert b"T-1004" in run2_resp.data

    def test_manual_accept_unmatched_reflected_in_new_run(self, client):
        self._seed_and_run(client)
        ledger_result_id = self._get_result_id("T-1010", "UNMATCHED_LEDGER")

        resolve_resp = client.post(
            f"/results/{ledger_result_id}/resolve",
            data={"action": "accept_unmatched", "resolved_by": "bob", "notes": "known internal transfer"},
            follow_redirects=True,
        )
        assert resolve_resp.status_code == 200
        assert b"resolution saved" in resolve_resp.data

        run2_resp = client.get("/runs/2?status=MANUALLY_RESOLVED")
        assert b"T-1010" in run2_resp.data

    def test_resolution_persists_into_a_third_run(self, client):
        self._seed_and_run(client)
        ledger_result_id = self._get_result_id("T-1004", "UNMATCHED_LEDGER")

        from src.db import session_scope
        from src.models import SourceSystem, Transaction
        from sqlalchemy import select

        with session_scope() as session:
            statement_tx_id = session.execute(
                select(Transaction).where(
                    Transaction.source_system == SourceSystem.STATEMENT,
                    Transaction.natural_key == "C-9001",
                )
            ).scalar_one().id

        client.post(
            f"/results/{ledger_result_id}/resolve",
            data={
                "action": "match_with",
                "resolved_by": "alice",
                "counterpart_transaction_id": str(statement_tx_id),
            },
            follow_redirects=True,
        )

        # Run reconciliation again via the dashboard button - a third run.
        client.post("/runs", data={"triggered_by": "carol"}, follow_redirects=True)
        run3_resp = client.get("/runs/3?status=MANUALLY_RESOLVED")
        assert b"T-1004" in run3_resp.data

    def test_resolving_without_a_name_shows_error(self, client):
        self._seed_and_run(client)
        ledger_result_id = self._get_result_id("T-1004", "UNMATCHED_LEDGER")
        resp = client.post(
            f"/results/{ledger_result_id}/resolve",
            data={"action": "accept_unmatched", "resolved_by": ""},
            follow_redirects=True,
        )
        assert b"enter your name" in resp.data

    def test_cannot_resolve_an_already_matched_result(self, client):
        self._seed_and_run(client)
        matched_result_id = self._get_result_id("T-1001", "MATCHED")
        resp = client.post(
            f"/results/{matched_result_id}/resolve",
            data={"action": "accept_unmatched", "resolved_by": "alice"},
            follow_redirects=True,
        )
        assert b"not eligible" in resp.data
