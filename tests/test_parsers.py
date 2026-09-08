from decimal import Decimal

from src.ingestion.ledger_parser import LedgerParser
from src.ingestion.statement_parser import StatementParser
from src.ingestion.registry import detect_source_system, get_parser, known_systems

LEDGER_CSV = """trade_id,traded_at,instrument,side,quantity,price,gross_amount,state
T-1001,2025-07-01T09:15:00Z,BTC-USD,BUY,0.50,62000.00,31000.00,SETTLED
T-1011,2025-07-04T10:15:00Z,ETH-USD,BUY,10.00,3400.00,34000.00,SETTLED
"""

STATEMENT_CSV = """reference,executed_at,symbol,direction,qty,unit_price,total,status
T-1001,2025-07-01 09:15:00,BTC-USD,B,0.5,62000,31000.00,SETTLED
T-1011,2025-07-04 10:15:00,ETH-USD,B,10,3417,34170.00,SETTLED
"""


class TestLedgerParser:
    def test_parses_valid_rows(self):
        result = LedgerParser().parse(LEDGER_CSV)
        assert len(result.rows) == 2
        assert not result.errors

        first = result.rows[0]
        assert first.natural_key == "T-1001"
        assert first.instrument == "BTC-USD"
        assert first.side == "BUY"
        assert first.quantity == Decimal("0.50")
        assert first.price == Decimal("62000.00")
        assert first.gross_amount == Decimal("31000.00")
        assert first.state == "SETTLED"

    def test_missing_column_is_fatal(self):
        broken = "trade_id,traded_at,instrument,side,quantity,price,state\nT-1,2025-07-01T09:00:00Z,BTC-USD,BUY,1,1,SETTLED\n"
        result = LedgerParser().parse(broken)
        assert result.is_fatal
        assert not result.rows

    def test_bad_row_is_reported_but_others_still_parse(self):
        csv_text = (
            "trade_id,traded_at,instrument,side,quantity,price,gross_amount,state\n"
            "T-1,2025-07-01T09:00:00Z,BTC-USD,BUY,1,100,100,SETTLED\n"
            "T-2,2025-07-01T09:00:00Z,BTC-USD,SIDEWAYS,1,100,100,SETTLED\n"
            "T-3,2025-07-01T09:00:00Z,BTC-USD,SELL,2,100,200,SETTLED\n"
        )
        result = LedgerParser().parse(csv_text)
        assert len(result.rows) == 2
        assert len(result.errors) == 1
        assert result.errors[0].row_number == 2
        assert not result.is_fatal

    def test_empty_file(self):
        result = LedgerParser().parse("")
        assert result.is_fatal

    def test_header_casing_does_not_break_row_parsing(self):
        # Regression test: header validation used to be case-insensitive
        # but row field access wasn't, so a differently-cased-but-valid
        # header caused every row to fail with a misleading "missing" error.
        csv_text = (
            "Trade_ID,Traded_At,Instrument,Side,Quantity,Price,Gross_Amount,State\n"
            "T-1,2025-07-01T09:00:00Z,BTC-USD,BUY,1,100,100,SETTLED\n"
        )
        result = LedgerParser().parse(csv_text)
        assert not result.errors
        assert len(result.rows) == 1
        assert result.rows[0].natural_key == "T-1"


class TestStatementParser:
    def test_parses_and_normalizes_direction_and_timestamp(self):
        result = StatementParser().parse(STATEMENT_CSV)
        assert len(result.rows) == 2
        assert not result.errors

        first = result.rows[0]
        assert first.natural_key == "T-1001"
        assert first.side == "BUY"  # "B" normalized
        assert first.gross_amount == Decimal("31000.00")

    def test_price_discrepancy_is_preserved_not_corrected(self):
        # This is exactly the scenario in the assignment: same trade, but the
        # statement recorded a different price/total than the ledger. The
        # parser's job is only to normalize format, not to judge or fix values.
        result = StatementParser().parse(STATEMENT_CSV)
        eth_row = next(r for r in result.rows if r.natural_key == "T-1011")
        assert eth_row.price == Decimal("3417")
        assert eth_row.gross_amount == Decimal("34170.00")

    def test_header_casing_does_not_break_row_parsing(self):
        csv_text = (
            "Reference,Executed_At,Symbol,Direction,Qty,Unit_Price,Total,Status\n"
            "T-1,2025-07-01 09:00:00,BTC-USD,B,1,100,100,SETTLED\n"
        )
        result = StatementParser().parse(csv_text)
        assert not result.errors
        assert len(result.rows) == 1
        assert result.rows[0].natural_key == "T-1"


class TestRegistry:
    def test_known_systems_include_both_formats(self):
        assert set(known_systems()) == {"LEDGER", "STATEMENT"}

    def test_detects_ledger_header(self):
        assert detect_source_system(LEDGER_CSV) == "LEDGER"

    def test_detects_statement_header(self):
        assert detect_source_system(STATEMENT_CSV) == "STATEMENT"

    def test_unrecognized_header_returns_none(self):
        assert detect_source_system("foo,bar\n1,2\n") is None

    def test_get_parser_roundtrip(self):
        parser = get_parser("LEDGER")
        assert isinstance(parser, LedgerParser)


class TestThirdSourceExtensibility:
    """
    The assignment's core extensibility requirement: "Tomorrow there may be
    a third company sending a third format." Proves that supporting one
    means writing one new SourceParser and registering it - nothing else
    (the import service, the matching engine, the UI) needs to change.
    """

    def test_a_third_source_format_can_be_added_without_touching_existing_parsers(self):
        from src.ingestion.base import NormalizedRow, ParseResult, RowError, SourceParser
        from src.ingestion.normalize import (
            normalize_decimal,
            normalize_instrument,
            normalize_natural_key,
            normalize_side,
            normalize_state,
            normalize_timestamp,
        )
        from src.ingestion.registry import get_parser, known_systems, register_parser

        class ThirdPartyParser(SourceParser):
            """A hypothetical third exchange's pipe-delimited format."""
            system_name = "THIRDPARTY"
            expected_columns = ("id", "when", "sym", "dir", "amt", "px", "value", "st")

            def parse(self, csv_text: str) -> ParseResult:
                import csv
                import io

                result = ParseResult()
                reader = csv.DictReader(io.StringIO(csv_text), delimiter="|")
                for row_number, row in enumerate(reader, start=1):
                    try:
                        result.rows.append(NormalizedRow(
                            natural_key=normalize_natural_key(row.get("id")),
                            instrument=normalize_instrument(row.get("sym")),
                            side=normalize_side(row.get("dir")),
                            quantity=normalize_decimal(row.get("amt"), "amt"),
                            price=normalize_decimal(row.get("px"), "px"),
                            gross_amount=normalize_decimal(row.get("value"), "value"),
                            transacted_at=normalize_timestamp(row.get("when")),
                            state=normalize_state(row.get("st")),
                        ))
                    except ValueError as exc:
                        result.errors.append(RowError(row_number=row_number, message=str(exc)))
                return result

        register_parser(ThirdPartyParser())
        try:
            assert "THIRDPARTY" in known_systems()
            assert isinstance(get_parser("THIRDPARTY"), ThirdPartyParser)

            third_party_csv = "id|when|sym|dir|amt|px|value|st\nX-1|2025-07-01T09:15:00Z|BTC-USD|B|0.5|62000|31000|SETTLED\n"
            result = get_parser("THIRDPARTY").parse(third_party_csv)
            assert not result.errors
            assert len(result.rows) == 1
            row = result.rows[0]
            # Same normalized shape as every other source: BUY, not "B".
            assert row.natural_key == "X-1"
            assert row.side == "BUY"
            assert row.instrument == "BTC-USD"

            # The other two parsers are completely unaffected.
            assert isinstance(get_parser("LEDGER"), LedgerParser)
            assert isinstance(get_parser("STATEMENT"), StatementParser)
        finally:
            # Don't leak this test parser into other tests in the same process.
            from src.ingestion import registry
            del registry._PARSERS["THIRDPARTY"]
