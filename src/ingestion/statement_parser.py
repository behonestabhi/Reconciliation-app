"""
Parser for the counterparty's statement CSV export.

Expected columns:
    reference,executed_at,symbol,direction,qty,unit_price,total,status

Same underlying fields as the ledger format, different names and different
value spellings (e.g. "B"/"S" instead of "BUY"/"SELL", space-separated
timestamps instead of ISO 8601).
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


class StatementParser(SourceParser):
    system_name = "STATEMENT"
    expected_columns = ("reference", "executed_at", "symbol", "direction", "qty", "unit_price", "total", "status")

    def parse(self, csv_text: str) -> ParseResult:
        result = ParseResult()
        reader = csv.DictReader(io.StringIO(csv_text))

        if reader.fieldnames is None:
            result.errors.append(RowError(row_number=0, message="file is empty or has no header row"))
            return result

        # Normalize header casing/whitespace once, here, so every row lookup
        # below (which uses the lowercase expected_columns names) actually
        # finds its value regardless of how the source file capitalized its
        # header - "Symbol" and "symbol" must both work.
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
                        natural_key=normalize_natural_key(row.get("reference")),
                        instrument=normalize_instrument(row.get("symbol")),
                        side=normalize_side(row.get("direction")),
                        quantity=normalize_decimal(row.get("qty"), "qty"),
                        price=normalize_decimal(row.get("unit_price"), "unit_price"),
                        gross_amount=normalize_decimal(row.get("total"), "total"),
                        transacted_at=normalize_timestamp(row.get("executed_at")),
                        state=normalize_state(row.get("status")),
                    )
                )
            except ValueError as exc:
                result.errors.append(RowError(row_number=row_number, message=str(exc)))

        return result
