from datetime import timezone
from decimal import Decimal

import pytest

from src.ingestion.normalize import (
    normalize_decimal,
    normalize_instrument,
    normalize_natural_key,
    normalize_side,
    normalize_state,
    normalize_timestamp,
)


class TestNormalizeSide:
    def test_buy_word_form(self):
        assert normalize_side("BUY") == "BUY"

    def test_buy_letter_form(self):
        assert normalize_side("B") == "BUY"

    def test_sell_word_form(self):
        assert normalize_side("SELL") == "SELL"

    def test_sell_letter_form(self):
        assert normalize_side("S") == "SELL"

    def test_is_case_insensitive_and_trims_whitespace(self):
        assert normalize_side(" b ") == "BUY"
        assert normalize_side("sell") == "SELL"

    def test_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            normalize_side("MAYBE")

    def test_rejects_missing_value(self):
        with pytest.raises(ValueError):
            normalize_side(None)


class TestNormalizeState:
    def test_settled(self):
        assert normalize_state("SETTLED") == "SETTLED"

    def test_cancelled_both_spellings(self):
        assert normalize_state("CANCELLED") == "CANCELLED"
        assert normalize_state("CANCELED") == "CANCELLED"

    def test_unknown_value_is_preserved_verbatim_not_collapsed(self):
        # An unrecognized-but-present status shouldn't kill the whole row,
        # and must not be collapsed into a shared "unknown" bucket - two
        # different unrecognized statuses must never compare as equal.
        assert normalize_state("PARTIAL") == "PARTIAL"
        assert normalize_state("expired") == "EXPIRED"

    def test_missing_value_is_an_error(self):
        with pytest.raises(ValueError):
            normalize_state("")


class TestNormalizeTimestamp:
    def test_iso_with_z_suffix(self):
        dt = normalize_timestamp("2025-07-01T09:15:00Z")
        assert dt.tzinfo is not None
        assert dt.astimezone(timezone.utc).hour == 9

    def test_space_separated_statement_format(self):
        dt = normalize_timestamp("2025-07-01 09:15:00")
        assert dt.astimezone(timezone.utc).hour == 9
        assert dt.astimezone(timezone.utc).minute == 15

    def test_naive_timestamps_assumed_utc(self):
        a = normalize_timestamp("2025-07-01T09:15:00Z")
        b = normalize_timestamp("2025-07-01 09:15:00")
        assert a == b

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            normalize_timestamp("not a date")

    def test_rejects_missing(self):
        with pytest.raises(ValueError):
            normalize_timestamp("")


class TestNormalizeDecimal:
    def test_parses_plain_number(self):
        assert normalize_decimal("62000.00") == Decimal("62000.00")

    def test_parses_integer_looking_value(self):
        assert normalize_decimal("62000") == Decimal("62000")

    def test_strips_thousands_separators(self):
        assert normalize_decimal("1,234.56") == Decimal("1234.56")

    def test_returns_decimal_not_float(self):
        # This matters: float(0.1 + 0.2) != 0.3, Decimal comparisons are exact.
        assert isinstance(normalize_decimal("0.1"), Decimal)

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError):
            normalize_decimal("not-a-number")

    def test_rejects_missing(self):
        with pytest.raises(ValueError):
            normalize_decimal(None)


class TestNormalizeInstrumentAndKey:
    def test_instrument_uppercased_and_trimmed(self):
        assert normalize_instrument(" btc-usd ") == "BTC-USD"

    def test_natural_key_trimmed(self):
        assert normalize_natural_key(" T-1001 ") == "T-1001"

    def test_natural_key_rejects_empty(self):
        with pytest.raises(ValueError):
            normalize_natural_key("   ")
