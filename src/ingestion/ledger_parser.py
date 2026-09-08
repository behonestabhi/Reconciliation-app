"""
Parser for our own ledger's CSV export.

Expected columns:
    trade_id,traded_at,instrument,side,quantity,price,gross_amount,state
"""
import csv
import io

from src.ingestion.base import NormalizedRow, ParseResult, RowError, SourceParser
from src.ingestion.normalize import (
    normalize_decimal,
    normalize_instrument,
    normalize_natural_key,
    normalize_side,
    normalize_state,
    normalize_timestamp,
)


class LedgerParser(SourceParser):
    system_name = "LEDGER"
    expected_columns = ("trade_id", "traded_at", "instrument", "side", "quantity", "price", "gross_amount", "state")

    def parse(self, csv_text: str) -> ParseResult:
        result = ParseResult()
        reader = csv.DictReader(io.StringIO(csv_text))

        if reader.fieldnames is None:
            result.errors.append(RowError(row_number=0, message="file is empty or has no header row"))
            return result

        # Normalize header casing/whitespace once, here, so every row lookup
        # below (which uses the lowercase expected_columns names) actually
        # finds its value regardless of how the source file capitalized its
        # header - "Trade_ID" and "trade_id" must both work.
        reader.fieldnames = [c.strip().lower() for c in reader.fieldnames]

        missing = set(self.expected_columns) - set(reader.fieldnames)
        if missing:
            result.errors.append(
                RowError(
                    row_number=0,
                    message=f"missing expected column(s): {', '.join(sorted(missing))}",
                )
            )
            return result

        for row_number, row in enumerate(reader, start=1):
            try:
                result.rows.append(
                    NormalizedRow(
                        natural_key=normalize_natural_key(row.get("trade_id")),
                        instrument=normalize_instrument(row.get("instrument")),
                        side=normalize_side(row.get("side")),
                        quantity=normalize_decimal(row.get("quantity"), "quantity"),
                        price=normalize_decimal(row.get("price"), "price"),
                        gross_amount=normalize_decimal(row.get("gross_amount"), "gross_amount"),
                        transacted_at=normalize_timestamp(row.get("traded_at")),
                        state=normalize_state(row.get("state")),
                    )
                )
            except ValueError as exc:
                result.errors.append(RowError(row_number=row_number, message=str(exc)))

        return result
