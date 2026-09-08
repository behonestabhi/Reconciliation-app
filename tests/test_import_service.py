from decimal import Decimal

from sqlalchemy import select

from src.models import FileStatus, SourceSystem, Transaction, TransactionVersion
from src.services.import_service import import_file
from tests.conftest import read_sample_bytes


class TestBasicImport:
    def test_imports_all_rows_and_creates_transactions(self, db_session):
        content = read_sample_bytes("ledger_2025-07.csv")
        source_file = import_file(db_session, "ledger_2025-07.csv", content, source_system="LEDGER")

        assert source_file.status == FileStatus.PROCESSED
        assert source_file.rows_created == 10
        assert source_file.rows_corrected == 0
        assert source_file.rows_failed == 0

        transactions = db_session.execute(select(Transaction)).scalars().all()
        assert len(transactions) == 10

        t1001 = db_session.execute(
            select(Transaction).where(Transaction.natural_key == "T-1001")
        ).scalar_one()
        assert t1001.source_system == SourceSystem.LEDGER
        assert t1001.instrument == "BTC-USD"
        assert Decimal(t1001.gross_amount) == Decimal("31000.00")

    def test_each_new_transaction_gets_a_first_version(self, db_session):
        content = read_sample_bytes("ledger_2025-07.csv")
        import_file(db_session, "ledger_2025-07.csv", content, source_system="LEDGER")

        t1001 = db_session.execute(
            select(Transaction).where(Transaction.natural_key == "T-1001")
        ).scalar_one()
        versions = db_session.execute(
            select(TransactionVersion).where(TransactionVersion.transaction_id == t1001.id)
        ).scalars().all()
        assert len(versions) == 1
        assert versions[0].version_number == 1
        assert versions[0].effective_to is None

    def test_auto_detects_format_when_not_specified(self, db_session):
        content = read_sample_bytes("statement_2025-07.csv")
        source_file = import_file(db_session, "statement_2025-07.csv", content, source_system=None)
        assert source_file.source_system == SourceSystem.STATEMENT
        assert source_file.status == FileStatus.PROCESSED


class TestDuplicateDetection:
    def test_reimporting_identical_content_is_flagged_duplicate(self, db_session):
        content = read_sample_bytes("ledger_2025-07.csv")
        first = import_file(db_session, "ledger_2025-07.csv", content, source_system="LEDGER")
        second = import_file(db_session, "ledger_2025-07_resend.csv", content, source_system="LEDGER")

        assert first.status == FileStatus.PROCESSED
        assert second.status == FileStatus.DUPLICATE
        assert str(first.id) in second.error_summary

    def test_duplicate_import_does_not_create_more_transactions(self, db_session):
        content = read_sample_bytes("ledger_2025-07.csv")
        import_file(db_session, "ledger_2025-07.csv", content, source_system="LEDGER")
        import_file(db_session, "ledger_2025-07_again.csv", content, source_system="LEDGER")

        transactions = db_session.execute(select(Transaction)).scalars().all()
        assert len(transactions) == 10  # not 20

    def test_different_content_is_not_a_duplicate(self, db_session):
        ledger = read_sample_bytes("ledger_2025-07.csv")
        statement = read_sample_bytes("statement_2025-07.csv")
        first = import_file(db_session, "ledger_2025-07.csv", ledger, source_system="LEDGER")
        second = import_file(db_session, "statement_2025-07.csv", statement, source_system="STATEMENT")
        assert first.status == FileStatus.PROCESSED
        assert second.status == FileStatus.PROCESSED

    def test_the_same_file_can_be_reuploaded_many_times_without_a_db_error(self, db_session):
        # Regression test: duplicate SourceFile rows used to be given a
        # synthetic "hash#dup<id>" value to work around the unique
        # constraint, which both exceeded the column's declared width and
        # meant a second and third duplicate attempt could collide with each
        # other. content_hash is now nullable for duplicate rows instead, so
        # this must succeed cleanly any number of times.
        content = read_sample_bytes("ledger_2025-07.csv")
        import_file(db_session, "a.csv", content, source_system="LEDGER")
        second = import_file(db_session, "b.csv", content, source_system="LEDGER")
        third = import_file(db_session, "c.csv", content, source_system="LEDGER")
        assert second.status == FileStatus.DUPLICATE
        assert third.status == FileStatus.DUPLICATE
        assert second.content_hash is None
        assert third.content_hash is None


class TestCorrectionFiles:
    def test_correction_updates_current_values_and_flags_file(self, db_session):
        base = read_sample_bytes("ledger_2025-07.csv")
        correction = read_sample_bytes("ledger_2025-07_correction.csv")

        import_file(db_session, "ledger_2025-07.csv", base, source_system="LEDGER")
        corrected_file = import_file(db_session, "ledger_2025-07_correction.csv", correction, source_system="LEDGER")

        assert corrected_file.is_correction is True
        assert corrected_file.rows_corrected == 1  # only T-1008 changed
        assert corrected_file.rows_unchanged == 9

        t1008 = db_session.execute(
            select(Transaction).where(Transaction.natural_key == "T-1008")
        ).scalar_one()
        assert Decimal(t1008.quantity) == Decimal("0.06")
        assert Decimal(t1008.gross_amount) == Decimal("3870.00")

    def test_correction_preserves_previous_value_in_history(self, db_session):
        base = read_sample_bytes("ledger_2025-07.csv")
        correction = read_sample_bytes("ledger_2025-07_correction.csv")

        import_file(db_session, "ledger_2025-07.csv", base, source_system="LEDGER")
        import_file(db_session, "ledger_2025-07_correction.csv", correction, source_system="LEDGER")

        t1008 = db_session.execute(
            select(Transaction).where(Transaction.natural_key == "T-1008")
        ).scalar_one()
        versions = db_session.execute(
            select(TransactionVersion)
            .where(TransactionVersion.transaction_id == t1008.id)
            .order_by(TransactionVersion.version_number)
        ).scalars().all()

        assert len(versions) == 2
        assert versions[0].version_number == 1
        assert Decimal(versions[0].quantity) == Decimal("0.05")  # what it used to say
        assert versions[0].effective_to is not None  # closed out

        assert versions[1].version_number == 2
        assert Decimal(versions[1].quantity) == Decimal("0.06")  # current value
        assert versions[1].effective_to is None

    def test_unchanged_rows_do_not_grow_history(self, db_session):
        base = read_sample_bytes("ledger_2025-07.csv")
        correction = read_sample_bytes("ledger_2025-07_correction.csv")

        import_file(db_session, "ledger_2025-07.csv", base, source_system="LEDGER")
        import_file(db_session, "ledger_2025-07_correction.csv", correction, source_system="LEDGER")

        t1001 = db_session.execute(
            select(Transaction).where(Transaction.natural_key == "T-1001")
        ).scalar_one()
        versions = db_session.execute(
            select(TransactionVersion).where(TransactionVersion.transaction_id == t1001.id)
        ).scalars().all()
        assert len(versions) == 1  # re-sent identically, no new version


class TestMalformedFiles:
    def test_non_utf8_content_fails_cleanly_instead_of_crashing(self, db_session):
        # Regression test: a binary/non-UTF-8 upload used to raise an
        # unhandled UnicodeDecodeError instead of being recorded as a
        # normal failed import.
        bad_bytes = b"\xff\xfe\x00\x01 not valid utf-8 \x80\x81"
        source_file = import_file(db_session, "garbage.csv", bad_bytes, source_system="LEDGER")
        assert source_file.status == FileStatus.FAILED
        assert "UTF-8" in source_file.error_summary

    def test_valid_rows_are_kept_and_invalid_rows_are_reported(self, db_session):
        content = read_sample_bytes("ledger_2025-07-05_malformed.csv")
        source_file = import_file(db_session, "ledger_2025-07-05_malformed.csv", content, source_system="LEDGER")

        assert source_file.status == FileStatus.PROCESSED_WITH_ERRORS
        assert source_file.rows_created == 2  # T-2001, T-2005
        assert source_file.rows_failed == 3   # bad side, bad quantity, missing timestamp
        assert "row 2" in source_file.error_summary
        assert "row 3" in source_file.error_summary
        assert "row 4" in source_file.error_summary

    def test_only_valid_rows_become_transactions(self, db_session):
        content = read_sample_bytes("ledger_2025-07-05_malformed.csv")
        import_file(db_session, "ledger_2025-07-05_malformed.csv", content, source_system="LEDGER")
        transactions = db_session.execute(select(Transaction)).scalars().all()
        keys = {t.natural_key for t in transactions}
        assert keys == {"T-2001", "T-2005"}

    def test_unrecognized_format_fails_cleanly(self, db_session):
        content = b"foo,bar\n1,2\n"
        source_file = import_file(db_session, "mystery.csv", content, source_system=None)
        assert source_file.status == FileStatus.FAILED
        assert "detect" in source_file.error_summary
