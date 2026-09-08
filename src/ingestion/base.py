"""
Shared contract for source parsers.

Every parser turns raw CSV text into a ``ParseResult``: a list of
``NormalizedRow`` (clean, uniform, ready to persist) plus a list of
``RowError`` for anything that couldn't be parsed. Nothing here touches a
database or a web framework, which is what makes ``parser.parse(text)``
testable with nothing more than a string.

Adding a third source later means writing one new class that implements
``SourceParser`` and registering it in ``registry.py`` -- everything else
(the import service, the matching engine) is agnostic to source format.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class NormalizedRow:
    """One trade, in a shape common to every source system."""
    natural_key: str
    instrument: str
    side: str            # "BUY" | "SELL"
    quantity: Decimal
    price: Decimal
    gross_amount: Decimal
    transacted_at: datetime
    state: str            # canonical "SETTLED" | "CANCELLED" | "PENDING", or raw text if unrecognized


@dataclass(frozen=True)
class RowError:
    """A row that could not be normalized, and why."""
    row_number: int        # 1-based, counting header as row 0
    message: str


@dataclass
class ParseResult:
    rows: list[NormalizedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def is_fatal(self) -> bool:
        """True if nothing usable came out of the file at all."""
        return not self.rows and bool(self.errors)


class SourceParser(ABC):
    """Base class every source-format parser implements."""

    #: Identifier stored on SourceFile / used for registry lookup, e.g. "LEDGER".
    system_name: str = ""

    #: Column names (lowercased) this parser expects to see in the header,
    #: used by the registry to auto-detect format for unlabeled files.
    expected_columns: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, csv_text: str) -> ParseResult:
        """Parse raw CSV text into normalized rows plus any row errors."""
        raise NotImplementedError

    def matches_header(self, header_columns: list[str]) -> bool:
        """Used by the registry's format auto-detection."""
        lowered = {c.strip().lower() for c in header_columns}
        return set(self.expected_columns).issubset(lowered)
