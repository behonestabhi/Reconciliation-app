"""
Tests for the pure matching/comparison engine in src/matching/core.py.

Deliberately construct everything as plain MatchInput objects - no
database, no Flask - to prove the reconciliation logic is testable in
complete isolation, as the assignment requires.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from src.matching.core import (
    ACCEPTED_AS_UNMATCHED,
    DIFFERS,
    MANUALLY_RESOLVED,
    MATCHED,
    MATCHED_MANUALLY,
    MATCHED_WITHIN_TOLERANCE,
    UNMATCHED_LEDGER,
    UNMATCHED_STATEMENT,
    ManualLink,
    MatchInput,
    compare_pair,
    run_matching,
)

BASE_TIME = datetime(2025, 7, 1, 9, 15, 0)


def make_row(key="T-1001", instrument="BTC-USD", side="BUY", quantity="0.5", price="62000.00",
             gross_amount="31000.00", transacted_at=BASE_TIME, state="SETTLED", ref_id=None) -> MatchInput:
    return MatchInput(
        key=key,
        instrument=instrument,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        gross_amount=Decimal(gross_amount),
        transacted_at=transacted_at,
        state=state,
        ref_id=ref_id,
    )


class TestExactMatch:
    def test_identical_rows_match_exactly(self):
        ledger = make_row()
        statement = make_row()
        outcomes = run_matching([ledger], [statement])
        assert len(outcomes) == 1
        assert outcomes[0].status == MATCHED
        assert outcomes[0].diffs == []

    def test_exact_match_is_distinguished_from_tolerance_match(self):
        # Same values -> MATCHED, not MATCHED_WITHIN_TOLERANCE.
        outcomes = run_matching([make_row()], [make_row()])
        assert outcomes[0].status == MATCHED


class TestSideNormalization:
    """
    BUY/B and SELL/S normalization itself happens in the ingestion layer
    (tests/test_normalize.py). By the time rows reach the matching engine
    they're already normalized to BUY/SELL - these tests confirm the engine
    correctly treats matching normalized sides as identical, and different
    sides as a real (significant) discrepancy.
    """
    def test_buy_matches_buy(self):
        ledger = make_row(side="BUY")
        statement = make_row(side="BUY")  # would have arrived as "B" pre-normalization
        outcomes = run_matching([ledger], [statement])
        assert outcomes[0].status == MATCHED

    def test_sell_matches_sell(self):
        ledger = make_row(side="SELL")
        statement = make_row(side="SELL")  # would have arrived as "S" pre-normalization
        outcomes = run_matching([ledger], [statement])
        assert outcomes[0].status == MATCHED

    def test_mismatched_side_is_significant(self):
        ledger = make_row(side="BUY")
        statement = make_row(side="SELL")
        outcomes = run_matching([ledger], [statement])
        assert outcomes[0].status == DIFFERS
        side_diff = next(d for d in outcomes[0].diffs if d.field_name == "side")
        assert side_diff.is_significant
        assert side_diff.ledger_value == "BUY"
        assert side_diff.statement_value == "SELL"


class TestAmountTolerance:
    def test_amount_within_tolerance_is_not_significant(self):
        ledger = make_row(gross_amount="6350.00")
        statement = make_row(gross_amount="6350.01")
        outcomes = run_matching([ledger], [statement], amount_tolerance=Decimal("0.01"))
        assert outcomes[0].status == MATCHED_WITHIN_TOLERANCE
        diff = next(d for d in outcomes[0].diffs if d.field_name == "gross_amount")
        assert diff.is_significant is False
        assert diff.delta == Decimal("0.01")

    def test_amount_outside_tolerance_is_significant(self):
        ledger = make_row(gross_amount="17500.00")
        statement = make_row(gross_amount="17750.00")
        outcomes = run_matching([ledger], [statement], amount_tolerance=Decimal("0.01"))
        assert outcomes[0].status == DIFFERS
        diff = next(d for d in outcomes[0].diffs if d.field_name == "gross_amount")
        assert diff.is_significant is True
        assert diff.delta == Decimal("250.00")

    def test_amount_exactly_at_tolerance_boundary_is_not_significant(self):
        # Tolerance is inclusive: a delta equal to the tolerance is normal
        # drift, not a problem.
        ledger = make_row(price="100.00")
        statement = make_row(price="100.01")
        outcomes = run_matching([ledger], [statement], amount_tolerance=Decimal("0.01"))
        diff = next(d for d in outcomes[0].diffs if d.field_name == "price")
        assert diff.is_significant is False

    def test_reports_both_values_and_delta(self):
        ledger = make_row(gross_amount="17500.00")
        statement = make_row(gross_amount="17750.00")
        outcomes = run_matching([ledger], [statement])
        diff = next(d for d in outcomes[0].diffs if d.field_name == "gross_amount")
        assert diff.ledger_value == "17500.00"
        assert diff.statement_value == "17750.00"
        assert diff.delta == Decimal("250.00")

    def test_quantity_has_zero_tolerance(self):
        # Quantity isn't subject to rounding/fees the way money is; any
        # difference at all is significant.
        ledger = make_row(quantity="1.00")
        statement = make_row(quantity="1.001")
        outcomes = run_matching([ledger], [statement])
        diff = next(d for d in outcomes[0].diffs if d.field_name == "quantity")
        assert diff.is_significant is True


class TestTimestampTolerance:
    def test_timestamp_within_tolerance_is_not_significant(self):
        ledger = make_row(transacted_at=BASE_TIME)
        statement = make_row(transacted_at=BASE_TIME + timedelta(seconds=30))
        outcomes = run_matching([ledger], [statement], time_tolerance_seconds=60)
        assert outcomes[0].status == MATCHED_WITHIN_TOLERANCE
        diff = next(d for d in outcomes[0].diffs if d.field_name == "transacted_at")
        assert diff.is_significant is False

    def test_timestamp_outside_tolerance_is_significant(self):
        ledger = make_row(transacted_at=BASE_TIME)
        statement = make_row(transacted_at=BASE_TIME + timedelta(hours=3))
        outcomes = run_matching([ledger], [statement], time_tolerance_seconds=60)
        assert outcomes[0].status == DIFFERS
        diff = next(d for d in outcomes[0].diffs if d.field_name == "transacted_at")
        assert diff.is_significant is True
        assert diff.delta == Decimal("10800")  # 3 hours in seconds

    def test_timestamp_exactly_at_tolerance_boundary_is_not_significant(self):
        ledger = make_row(transacted_at=BASE_TIME)
        statement = make_row(transacted_at=BASE_TIME + timedelta(seconds=60))
        outcomes = run_matching([ledger], [statement], time_tolerance_seconds=60)
        diff = next(d for d in outcomes[0].diffs if d.field_name == "transacted_at")
        assert diff.is_significant is False


class TestMultipleFieldMismatches:
    def test_reports_every_differing_field_independently(self):
        ledger = make_row(price="3400.00", gross_amount="34000.00", side="BUY")
        statement = make_row(price="3417.00", gross_amount="34170.00", side="SELL")
        outcomes = run_matching([ledger], [statement])
        assert outcomes[0].status == DIFFERS

        field_names = {d.field_name for d in outcomes[0].diffs}
        assert field_names == {"price", "gross_amount", "side"}
        assert all(d.is_significant for d in outcomes[0].diffs)

    def test_mix_of_significant_and_insignificant_diffs_is_still_differs(self):
        # One real problem (price) plus one harmless rounding drift
        # (gross_amount within tolerance) - the pair is still DIFFERS
        # overall, but both diffs are individually reported with their own
        # significance.
        ledger = make_row(price="3400.00", gross_amount="6350.00")
        statement = make_row(price="3500.00", gross_amount="6350.01")
        outcomes = run_matching([ledger], [statement], amount_tolerance=Decimal("0.01"))
        assert outcomes[0].status == DIFFERS

        price_diff = next(d for d in outcomes[0].diffs if d.field_name == "price")
        amount_diff = next(d for d in outcomes[0].diffs if d.field_name == "gross_amount")
        assert price_diff.is_significant is True
        assert amount_diff.is_significant is False


class TestUnmatched:
    def test_ledger_row_with_no_counterpart_is_unmatched_ledger(self):
        ledger = make_row(key="T-1004")
        outcomes = run_matching([ledger], [])
        assert len(outcomes) == 1
        assert outcomes[0].status == UNMATCHED_LEDGER
        assert outcomes[0].ledger is ledger
        assert outcomes[0].statement is None

    def test_statement_row_with_no_counterpart_is_unmatched_statement(self):
        statement = make_row(key="C-9001")
        outcomes = run_matching([], [statement])
        assert len(outcomes) == 1
        assert outcomes[0].status == UNMATCHED_STATEMENT
        assert outcomes[0].statement is statement
        assert outcomes[0].ledger is None

    def test_unmatched_can_occur_in_both_directions_in_one_run(self):
        ledger_only = make_row(key="T-1004")
        statement_only = make_row(key="C-9001")
        matched_ledger = make_row(key="T-1001")
        matched_statement = make_row(key="T-1001")

        outcomes = run_matching(
            [ledger_only, matched_ledger],
            [statement_only, matched_statement],
        )
        statuses = {o.status for o in outcomes}
        assert statuses == {UNMATCHED_LEDGER, UNMATCHED_STATEMENT, MATCHED}


class TestCancelledTransactions:
    def test_cancelled_pair_is_excluded_entirely(self):
        ledger = make_row(key="T-1007", state="CANCELLED")
        statement = make_row(key="T-1007", state="CANCELLED")
        outcomes = run_matching([ledger], [statement])
        assert outcomes == []

    def test_cancelled_ledger_row_does_not_suppress_a_live_statement_row(self):
        # If the ledger row was cancelled but the statement still has an
        # active row under the same key, the statement row is not simply
        # dropped - it has no valid counterpart, so it's unmatched.
        ledger = make_row(key="T-1007", state="CANCELLED")
        statement = make_row(key="T-1007", state="SETTLED")
        outcomes = run_matching([ledger], [statement])
        assert len(outcomes) == 1
        assert outcomes[0].status == UNMATCHED_STATEMENT

    def test_cancelled_transaction_never_appears_in_manual_match_pool(self):
        ledger = make_row(key="T-1007", state="CANCELLED")
        statement = make_row(key="C-9999", state="SETTLED")
        link = ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1007", statement_key="C-9999")
        outcomes = run_matching([ledger], [statement], manual_links=[link])
        # The cancelled ledger row was filtered out before manual links were
        # even considered, so nothing gets manually matched to it.
        assert len(outcomes) == 1
        assert outcomes[0].status == UNMATCHED_STATEMENT


class TestManualMatching:
    def test_manually_linked_rows_with_different_keys_are_paired(self):
        ledger = make_row(key="T-1004")
        statement = make_row(key="C-9001")
        link = ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1004", statement_key="C-9001")
        outcomes = run_matching([ledger], [statement], manual_links=[link])
        assert len(outcomes) == 1
        assert outcomes[0].status == MANUALLY_RESOLVED
        assert outcomes[0].ledger is ledger
        assert outcomes[0].statement is statement

    def test_manual_match_still_reports_field_differences(self):
        ledger = make_row(key="T-1004", gross_amount="1000.00")
        statement = make_row(key="C-9001", gross_amount="1200.00")
        link = ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1004", statement_key="C-9001")
        outcomes = run_matching([ledger], [statement], manual_links=[link])
        assert outcomes[0].status == MANUALLY_RESOLVED  # human call wins regardless of diffs
        assert any(d.field_name == "gross_amount" for d in outcomes[0].diffs)

    def test_manual_match_takes_precedence_over_auto_match_by_key(self):
        # T-1001 exists on both sides and would normally auto-match, but a
        # human has manually linked the ledger side to a *different*
        # statement row - that decision wins.
        ledger = make_row(key="T-1001")
        auto_statement = make_row(key="T-1001")
        manual_statement = make_row(key="C-9001")
        link = ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1001", statement_key="C-9001")

        outcomes = run_matching([ledger], [auto_statement, manual_statement], manual_links=[link])
        # ledger T-1001 -> manually resolved with C-9001; the original
        # statement T-1001 is left over with no partner.
        by_status = {o.status: o for o in outcomes}
        assert by_status[MANUALLY_RESOLVED].statement.key == "C-9001"
        assert by_status[UNMATCHED_STATEMENT].statement.key == "T-1001"

    def test_accepted_as_unmatched_ledger_row_is_manually_resolved_not_unmatched(self):
        ledger = make_row(key="T-1004")
        link = ManualLink(resolution_type=ACCEPTED_AS_UNMATCHED, ledger_key="T-1004")
        outcomes = run_matching([ledger], [], manual_links=[link])
        assert len(outcomes) == 1
        assert outcomes[0].status == MANUALLY_RESOLVED
        assert outcomes[0].ledger is ledger
        assert outcomes[0].statement is None

    def test_accepted_as_unmatched_statement_row(self):
        statement = make_row(key="C-9001")
        link = ManualLink(resolution_type=ACCEPTED_AS_UNMATCHED, statement_key="C-9001")
        outcomes = run_matching([], [statement], manual_links=[link])
        assert outcomes[0].status == MANUALLY_RESOLVED
        assert outcomes[0].statement is statement


class TestDuplicatePreventionAndAmbiguity:
    def test_one_ledger_row_cannot_be_consumed_by_two_manual_links(self):
        # Two conflicting instructions reference the same ledger key - the
        # engine must not guess which one is "right"; it resolves neither.
        ledger = make_row(key="T-1004")
        statement_a = make_row(key="C-1")
        statement_b = make_row(key="C-2")
        links = [
            ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1004", statement_key="C-1"),
            ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-1004", statement_key="C-2"),
        ]
        outcomes = run_matching([ledger], [statement_a, statement_b], manual_links=links)
        statuses = [o.status for o in outcomes]
        assert MANUALLY_RESOLVED not in statuses
        # Both statement rows and the ledger row should fall through to
        # unmatched, since the conflicting instruction resolved nothing.
        assert sorted(statuses) == sorted([UNMATCHED_LEDGER, UNMATCHED_STATEMENT, UNMATCHED_STATEMENT])

    def test_auto_matching_never_double_consumes_a_row(self):
        # Sanity check: with unique keys, each row appears in exactly one
        # outcome.
        ledger_rows = [make_row(key=f"T-{i}") for i in range(5)]
        statement_rows = [make_row(key=f"T-{i}") for i in range(5)]
        outcomes = run_matching(ledger_rows, statement_rows)
        assert len(outcomes) == 5
        seen_ledger_keys = [o.ledger.key for o in outcomes if o.ledger]
        seen_statement_keys = [o.statement.key for o in outcomes if o.statement]
        assert len(seen_ledger_keys) == len(set(seen_ledger_keys))
        assert len(seen_statement_keys) == len(set(seen_statement_keys))

    def test_coincidental_same_string_on_opposite_sides_is_not_treated_as_a_conflict(self):
        # Ledger and statement natural_keys are independent namespaces: a
        # ledger_key that happens to equal some *other* link's
        # statement_key is not the same transaction and must not be
        # flagged as ambiguous or block either resolution.
        ledger_a = make_row(key="X-1")   # linked as a ledger_key below
        statement_a = make_row(key="C-1")
        ledger_b = make_row(key="T-2")
        statement_b = make_row(key="X-1")  # coincidentally the same string, but a *statement* key

        links = [
            ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="X-1", statement_key="C-1"),
            ManualLink(resolution_type=MATCHED_MANUALLY, ledger_key="T-2", statement_key="X-1"),
        ]
        outcomes = run_matching([ledger_a, ledger_b], [statement_a, statement_b], manual_links=links)
        resolved = {o.status for o in outcomes}
        assert resolved == {MANUALLY_RESOLVED}
        assert len(outcomes) == 2


class TestComparePairDirectly:
    def test_no_diffs_for_identical_rows(self):
        assert compare_pair(make_row(), make_row()) == []

    def test_instrument_mismatch_is_significant(self):
        diffs = compare_pair(make_row(instrument="BTC-USD"), make_row(instrument="ETH-USD"))
        assert len(diffs) == 1
        assert diffs[0].field_name == "instrument"
        assert diffs[0].is_significant is True

    def test_state_mismatch_is_significant(self):
        diffs = compare_pair(make_row(state="SETTLED"), make_row(state="PENDING"))
        assert len(diffs) == 1
        assert diffs[0].field_name == "state"
        assert diffs[0].is_significant is True

    def test_two_different_unrecognized_statuses_are_not_treated_as_equal(self):
        # Regression test: normalize_state used to collapse any unrecognized
        # status into a shared "OTHER" bucket, which made two genuinely
        # different unrecognized statuses compare as a false match on the
        # state field. Unrecognized statuses are now preserved verbatim.
        diffs = compare_pair(make_row(state="PARTIAL"), make_row(state="EXPIRED"))
        assert len(diffs) == 1
        assert diffs[0].field_name == "state"
        assert diffs[0].ledger_value == "PARTIAL"
        assert diffs[0].statement_value == "EXPIRED"
        assert diffs[0].is_significant is True
