"""
Pure normalization helpers shared by every parser.

Every function here takes a raw string (as read from a CSV cell) and
returns a normalized Python value, or raises ``ValueError`` with a message
describing what was wrong. Parsers catch ``ValueError`` and turn it into a
``RowError`` that points at the offending row and field, rather than
letting a bad file crash the import.

Keeping these pure (no DB, no I/O) is what makes them trivially unit
testable and reusable by any future third-party format.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Side (BUY/SELL)
# ---------------------------------------------------------------------------

_SIDE_MAP = {
    "BUY": "BUY",
    "B": "BUY",
    "SELL": "SELL",
    "S": "SELL",
}


def normalize_side(raw: str) -> str:
    if raw is None:
        raise ValueError("side is missing")
    key = raw.strip().upper()
    if key not in _SIDE_MAP:
        raise ValueError(f"unrecognized side/direction value: {raw!r}")
    return _SIDE_MAP[key]


# ---------------------------------------------------------------------------
# State / status
# ---------------------------------------------------------------------------

_STATE_MAP = {
    "SETTLED": "SETTLED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
    "PENDING": "PENDING",
}


def normalize_state(raw: str) -> str:
    """
    Canonicalize the well-known spellings (including the CANCELLED/CANCELED
    variants seen across sources). Anything else is returned verbatim
    (uppercased, trimmed) rather than collapsed into a generic "unknown"
    bucket -- two different unrecognized statuses must never compare as
    equal just because neither was recognized.
    """
    if raw is None or not raw.strip():
        raise ValueError("state/status is missing")
    key = raw.strip().upper()
    return _STATE_MAP.get(key, key)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def normalize_timestamp(raw: str) -> datetime:
    """
    Parse a timestamp from either source format (ISO 8601 with a "Z" or
    numeric offset, or the space-separated "YYYY-MM-DD HH:MM:SS" style with
    no offset) and return it as UTC. A timestamp with no offset is assumed
    to already be UTC, per this app's convention (see README > Assumptions).
    """
    if raw is None or not raw.strip():
        raise ValueError("timestamp is missing")
    text = raw.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unrecognized timestamp format: {raw!r}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Monetary / numeric values
# ---------------------------------------------------------------------------

def normalize_decimal(raw: str, field_name: str = "value") -> Decimal:
    """
    Parse a monetary/quantity value safely. Always returns Decimal (never
    float) so that comparisons and tolerances later on are exact rather than
    subject to binary floating-point rounding.
    """
    if raw is None or not str(raw).strip():
        raise ValueError(f"{field_name} is missing")
    text = str(raw).strip().replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a valid number: {raw!r}") from exc
    return value


def normalize_instrument(raw: str) -> str:
    if raw is None or not raw.strip():
        raise ValueError("instrument/symbol is missing")
    return raw.strip().upper()


def normalize_natural_key(raw: str) -> str:
    if raw is None or not raw.strip():
        raise ValueError("id/reference is missing")
    return raw.strip()
