"""
Registry mapping a source system name to its parser, plus best-effort
auto-detection from a CSV header row.

This is the extension point for "tomorrow there may be a third company
sending a third format": implement ``SourceParser``, add one line here, and
everything downstream (import service, matching engine) keeps working
unchanged.
"""
import csv
import io

from src.ingestion.base import SourceParser
from src.ingestion.ledger_parser import LedgerParser
from src.ingestion.statement_parser import StatementParser

_PARSERS: dict[str, SourceParser] = {
    LedgerParser.system_name: LedgerParser(),
    StatementParser.system_name: StatementParser(),
}


def register_parser(parser: SourceParser) -> None:
    """Add a new source format at runtime - the hook a future third source
    would use (see test_parsers.py::TestThirdSourceExtensibility for a
    worked example)."""
    _PARSERS[parser.system_name] = parser


def get_parser(system_name: str) -> SourceParser:
    try:
        return _PARSERS[system_name]
    except KeyError as exc:
        known = ", ".join(sorted(_PARSERS))
        raise ValueError(f"no parser registered for source system {system_name!r}. Known: {known}") from exc


def detect_source_system(csv_text: str) -> str | None:
    """
    Look at the header row and return the system_name of the first
    registered parser whose expected columns are a subset of what's present.
    Returns None if no parser recognizes the header (caller should then
    require the caller to specify the format explicitly, or reject the
    file).
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return None

    for parser in _PARSERS.values():
        if parser.matches_header(header):
            return parser.system_name
    return None


def known_systems() -> list[str]:
    return list(_PARSERS.keys())
