"""
The reconciliation engine: pairing ledger transactions against statement
transactions, and comparing paired rows field by field.

Nothing in this module touches a database or a web framework. It operates
purely on ``MatchInput`` (a plain snapshot of one transaction) and
``ManualLink`` (a plain description of a prior human decision), and returns
plain ``MatchOutcome`` objects. The DB-aware wrapper that loads Transactions,
calls this, and persists the result lives in
``src/services/reconciliation_service.py``.

Matching rules (from the assignment):
  - Cancelled transactions are excluded entirely; they were never meant to
    be compared.
  - Two rows with the same natural_key, one from each system, are a
    candidate pair.
  - A prior manual decision (a ManualLink) always takes precedence over
    natural_key auto-matching, because "whatever they decide must still
    hold tomorrow."
  - A paired row is DIFFERS if any field disagrees by more than its
    tolerance; otherwise it is MATCHED. Small amount/timing drift is
    expected and is not, by itself, a difference worth flagging.
  - Anything left over on one side only is UNMATCHED_LEDGER or
    UNMATCHED_STATEMENT.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

MATCHED = "MATCHED"
MATCHED_WITHIN_TOLERANCE = "MATCHED_WITHIN_TOLERANCE"
DIFFERS = "DIFFERS"
UNMATCHED_LEDGER = "UNMATCHED_LEDGER"
UNMATCHED_STATEMENT = "UNMATCHED_STATEMENT"
MANUALLY_RESOLVED = "MANUALLY_RESOLVED"

MATCHED_MANUALLY = "MATCHED_MANUALLY"
ACCEPTED_AS_UNMATCHED = "ACCEPTED_AS_UNMATCHED"

CANCELLED_STATE = "CANCELLED"

DEFAULT_AMOUNT_TOLERANCE = Decimal("0.01")
DEFAULT_TIME_TOLERANCE_SECONDS = 60

# Fields compared on every pair, and whether a difference is amount-like,
# time-like, or must match exactly.
_EXACT_FIELDS = ("instrument", "side", "state")


@dataclass(frozen=True)
class MatchInput:
    """A snapshot of one transaction, as seen by one system."""
    key: str                # natural_key - the join key between systems
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    gross_amount: Decimal
    transacted_at: datetime
    state: str
    ref_id: int | None = None  # opaque passthrough (e.g. a DB transaction id)


@dataclass(frozen=True)
class ManualLink:
    """A prior human decision, keyed on natural_key per system."""
    resolution_type: str                 # MATCHED_MANUALLY | ACCEPTED_AS_UNMATCHED
    ledger_key: str | None = None
    statement_key: str | None = None


@dataclass(frozen=True)
class FieldDiff:
    field_name: str
    ledger_value: str
    statement_value: str
    delta: Decimal | None
    is_significant: bool


@dataclass
class MatchOutcome:
    status: str
    ledger: MatchInput | None
    statement: MatchInput | None
    diffs: list[FieldDiff] = field(default_factory=list)


def compare_pair(
    ledger: MatchInput,
    statement: MatchInput,
    amount_tolerance: Decimal = DEFAULT_AMOUNT_TOLERANCE,
    time_tolerance_seconds: int = DEFAULT_TIME_TOLERANCE_SECONDS,
) -> list[FieldDiff]:
    """
    Compare one paired ledger/statement transaction field by field. Returns
    a FieldDiff for every field that doesn't match exactly -- even ones
    within tolerance -- so a person can always see what differs and by how
    much; ``is_significant`` is what should drive whether the pair as a
    whole counts as DIFFERS.
    """
    diffs: list[FieldDiff] = []

    for field_name in _EXACT_FIELDS:
        lv, sv = getattr(ledger, field_name), getattr(statement, field_name)
        if lv != sv:
            diffs.append(
                FieldDiff(field_name=field_name, ledger_value=str(lv), statement_value=str(sv),
                           delta=None, is_significant=True)
            )

    for field_name, tolerance in (("quantity", Decimal("0")), ("price", amount_tolerance),
                                   ("gross_amount", amount_tolerance)):
        lv: Decimal = getattr(ledger, field_name)
        sv: Decimal = getattr(statement, field_name)
        if lv != sv:
            delta = abs(lv - sv)
            diffs.append(
                FieldDiff(field_name=field_name, ledger_value=str(lv), statement_value=str(sv),
                           delta=delta, is_significant=delta > tolerance)
            )

    if ledger.transacted_at != statement.transacted_at:
        delta_seconds = abs((ledger.transacted_at - statement.transacted_at).total_seconds())
        diffs.append(
            FieldDiff(
                field_name="transacted_at",
                ledger_value=ledger.transacted_at.isoformat(),
                statement_value=statement.transacted_at.isoformat(),
                delta=Decimal(str(delta_seconds)),
                is_significant=delta_seconds > time_tolerance_seconds,
            )
        )

    return diffs


def _find_ambiguous_keys(manual_links: list[ManualLink]) -> tuple[set[str], set[str]]:
    """
    Return (ambiguous_ledger_keys, ambiguous_statement_keys): natural_keys
    referenced by more than one manual link, *within the same side*.

    Ledger keys and statement keys are independent namespaces -- two
    different systems can coincidentally use the same natural_key string
    for unrelated trades -- so a ledger_key and a statement_key that happen
    to be equal must never be treated as a conflict with each other. Only
    a genuine repeat on the same side (e.g. two links both claiming
    ledger_key="T-1004") is ambiguous.

    This should never happen if the service layer validates before
    persisting a ManualResolution (see resolution_service.py), but the
    engine is defensive: if the data ever says two different things about
    the same transaction, the safe behaviour is to resolve neither claim
    rather than silently picking one and guessing.
    """
    ledger_seen: dict[str, int] = {}
    statement_seen: dict[str, int] = {}
    for link in manual_links:
        if link.ledger_key is not None:
            ledger_seen[link.ledger_key] = ledger_seen.get(link.ledger_key, 0) + 1
        if link.statement_key is not None:
            statement_seen[link.statement_key] = statement_seen.get(link.statement_key, 0) + 1
    ambiguous_ledger = {key for key, count in ledger_seen.items() if count > 1}
    ambiguous_statement = {key for key, count in statement_seen.items() if count > 1}
    return ambiguous_ledger, ambiguous_statement


def _link_conflicts(
    link: ManualLink,
    ambiguous_ledger: set[str],
    ambiguous_statement: set[str],
    consumed_ledger: set[str],
    consumed_statement: set[str],
) -> bool:
    """
    True if this link's ledger side or statement side is either ambiguous
    (claimed by more than one link) or already claimed by an earlier link.
    Ledger and statement keys are checked against their own side only --
    never cross-checked -- since the two are independent namespaces.
    """
    if link.ledger_key is not None:
        if link.ledger_key in ambiguous_ledger or link.ledger_key in consumed_ledger:
            return True
    if link.statement_key is not None:
        if link.statement_key in ambiguous_statement or link.statement_key in consumed_statement:
            return True
    return False


def run_matching(
    ledger_rows: list[MatchInput],
    statement_rows: list[MatchInput],
    manual_links: list[ManualLink] = (),
    amount_tolerance: Decimal = DEFAULT_AMOUNT_TOLERANCE,
    time_tolerance_seconds: int = DEFAULT_TIME_TOLERANCE_SECONDS,
) -> list[MatchOutcome]:
    """
    Pair ledger and statement rows and compare each pair. Cancelled rows are
    dropped before anything else runs, on both sides.
    """
    ledger_by_key = {r.key: r for r in ledger_rows if r.state != CANCELLED_STATE}
    statement_by_key = {r.key: r for r in statement_rows if r.state != CANCELLED_STATE}

    ambiguous_ledger, ambiguous_statement = _find_ambiguous_keys(manual_links)

    consumed_ledger: set[str] = set()
    consumed_statement: set[str] = set()
    outcomes: list[MatchOutcome] = []

    # 1. Manual decisions take precedence over anything auto-matching would do.
    for link in manual_links:
        if _link_conflicts(link, ambiguous_ledger, ambiguous_statement, consumed_ledger, consumed_statement):
            # Either this transaction has conflicting instructions, or it
            # was already claimed by an earlier link - same safety
            # principle either way: resolve nothing rather than guess.
            continue

        if link.resolution_type == MATCHED_MANUALLY:
            ledger_row = ledger_by_key.get(link.ledger_key) if link.ledger_key else None
            statement_row = statement_by_key.get(link.statement_key) if link.statement_key else None
            if ledger_row is None or statement_row is None:
                # Can't honor a match where one side doesn't exist (e.g. it
                # was cancelled and filtered out above, or never imported).
                # Leave both rows, if either is present, to fall through to
                # normal auto-matching/unmatched handling below rather than
                # silently resolving only the side that does exist.
                continue
            diffs = compare_pair(ledger_row, statement_row, amount_tolerance, time_tolerance_seconds)
            outcomes.append(MatchOutcome(status=MANUALLY_RESOLVED, ledger=ledger_row,
                                          statement=statement_row, diffs=diffs))
            consumed_ledger.add(link.ledger_key)
            consumed_statement.add(link.statement_key)

        elif link.resolution_type == ACCEPTED_AS_UNMATCHED:
            if link.ledger_key:
                row = ledger_by_key.get(link.ledger_key)
                if row is not None:
                    outcomes.append(MatchOutcome(status=MANUALLY_RESOLVED, ledger=row, statement=None))
                    consumed_ledger.add(link.ledger_key)
            if link.statement_key:
                row = statement_by_key.get(link.statement_key)
                if row is not None:
                    outcomes.append(MatchOutcome(status=MANUALLY_RESOLVED, ledger=None, statement=row))
                    consumed_statement.add(link.statement_key)

    # 2. Auto-match whatever's left by natural_key. Natural keys are unique
    # per system (enforced at the DB layer), so this is always a
    # deterministic one-to-one pairing: at most one ledger row and one
    # statement row can ever share a given key.
    for key, ledger_row in ledger_by_key.items():
        if key in consumed_ledger:
            continue
        statement_row = statement_by_key.get(key)
        if statement_row is None or key in consumed_statement:
            continue
        diffs = compare_pair(ledger_row, statement_row, amount_tolerance, time_tolerance_seconds)
        if not diffs:
            status = MATCHED
        elif any(d.is_significant for d in diffs):
            status = DIFFERS
        else:
            status = MATCHED_WITHIN_TOLERANCE
        outcomes.append(MatchOutcome(status=status, ledger=ledger_row, statement=statement_row, diffs=diffs))
        consumed_ledger.add(key)
        consumed_statement.add(key)

    # 3. Whatever's left on either side has no counterpart at all.
    for key, ledger_row in ledger_by_key.items():
        if key not in consumed_ledger:
            outcomes.append(MatchOutcome(status=UNMATCHED_LEDGER, ledger=ledger_row, statement=None))
            consumed_ledger.add(key)

    for key, statement_row in statement_by_key.items():
        if key not in consumed_statement:
            outcomes.append(MatchOutcome(status=UNMATCHED_STATEMENT, ledger=None, statement=statement_row))
            consumed_statement.add(key)

    return outcomes
