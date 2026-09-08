# The Reconciliation Problem

A small web application that reconciles our own trade ledger against a
counterparty's statement: it imports both sides, matches transactions
between them, flags what disagrees (and by how much), excludes what should
never be compared, and gives a person a screen to resolve by hand whatever
the system couldn't match automatically - with those decisions holding on
every future run.

## What it does

- **Imports** CSV files from either system, normalizing different column
  names, different date formats, and different vocabularies (BUY/B,
  SELL/S) into one common shape.
- **Detects duplicate file uploads** (by content, not filename) and applies
  **correction files** (a resend where a few rows' values changed) while
  preserving the old values in history.
- **Matches** ledger transactions to statement transactions by their shared
  ID, deterministically and one-to-one, excluding cancelled transactions
  entirely.
- **Compares** every matched pair field by field, distinguishing an exact
  match, a match within tolerance (normal rounding/fee/clock drift), and a
  real discrepancy - reporting both values and the delta for anything that
  differs.
- Surfaces whatever's **left over on either side** (a ledger row with no
  statement counterpart, or vice versa) for a person to resolve.
- Lets a person **manually pair** two transactions the system couldn't
  match, or **accept** a row as genuinely having no pair - and that
  decision **persists**: every future reconciliation run honors it.

## Stack

- Python 3.12, Flask (thin HTTP layer), SQLAlchemy 2.0 (ORM + schema),
  SQLite (file-based, zero setup).
- Plain server-rendered Jinja templates, no JS framework.
- pytest for tests (122 tests: pure-logic unit tests, DB-integration tests,
  and end-to-end tests driving the actual Flask app through its test client
  against a real SQLite file).

No other runtime dependencies - requirements.txt is exactly Flask,
SQLAlchemy, and pytest.

## Setup

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
```

## How to run

```bash
python scripts/migrate.py     # 1. apply migrations (creates recon.db)
python app.py                 # 2. run the app -> http://127.0.0.1:5000
```

## How to run tests

```bash
python -m pytest              # all 122 tests
python -m pytest -v           # with individual test names
python -m pytest tests/test_matching_core.py   # just the pure engine
```

### Walking through the workflow

1. Open http://127.0.0.1:5000/ - the dashboard. Empty at first.
2. **Import files**: go to "Import files", upload data/sample/ledger_2025-07.csv
   (source LEDGER) and data/sample/statement_2025-07.csv (STATEMENT, or
   leave the format on auto-detect).
3. Back on the dashboard, click **"Run reconciliation now"**.
4. You land on the run's results page: filter pills for every status
   (Differs / Unmatched-ours / Unmatched-counterparty / Tolerance /
   Manually resolved / Matched), and a sort control ("needs attention
   first", transaction ID, or largest difference).
5. Click any row to open its **transaction detail page**: our ledger and
   the counterparty's statement side by side, a field-by-field table
   showing both values, the delta, the tolerance that applied, and whether
   it's within tolerance or not.
6. For an unmatched row (e.g. ledger T-1004, which has no statement
   counterpart by ID), the detail page offers a resolution form: pick a
   candidate from the other side's still-unmatched rows in this run
   (C-9001 is exactly this case - it's the assignment's own example) and
   submit, or accept it as genuinely unmatched. Either way you give your
   name and, optionally, a note.
7. Submitting immediately re-runs reconciliation and takes you to the new
   run, filtered to "Manually resolved," so you see the decision reflected
   right away.
8. Click **"Run reconciliation now"** again from the dashboard - the
   resolution still shows as manually resolved in the new run, proving it
   persisted rather than being a one-off patch to a single run's results.
9. Try data/sample/ledger_2025-07_correction.csv - a same-batch resend
   that fixes one row's quantity. Re-run reconciliation and open that row's
   detail page: a "Correction history" table appears showing the old value,
   the new (current) value, and which file changed it.

## Architecture

```
app.py                      Flask entrypoint
src/
  config.py                 DB URL, tolerances (AMOUNT_TOLERANCE, TIME_TOLERANCE_SECONDS)
  db.py                     engine/session
  models.py                 schema (see "Database design" below)
  ingestion/                pure parsing & normalization - no DB, no Flask
    base.py                 SourceParser interface, NormalizedRow, ParseResult, RowError
    normalize.py            field-level normalizers (side, state, timestamp, decimal)
    ledger_parser.py        our ledger's CSV format
    statement_parser.py     the counterparty's CSV format
    registry.py             format lookup + auto-detection (the "third source" extension point)
  matching/
    core.py                 pure reconciliation engine - no DB, no Flask
  services/
    import_service.py       DB-aware: hashing, duplicate detection, upsert/versioning
    reconciliation_service.py  runs the matching engine, persists a run
    resolution_service.py   manual match/accept, with validation guards
    view_service.py         read-model queries for the UI (computes nothing itself)
  web/
    routes.py                thin controllers: dashboard, imports, runs, results, resolve
    templates/
      base.html, dashboard.html, imports.html, run_detail.html, result_detail.html
migrations/
  0001_initial.sql           generated from models.py (see "Migrations" below)
scripts/
  generate_migration.py      regenerate a migration from the current models
  migrate.py                 apply pending migrations, tracked in schema_migrations
data/sample/                 sample CSVs covering the scenarios below
tests/
  test_normalize.py                pure normalizer unit tests
  test_parsers.py                  parser unit tests, incl. a worked third-source example
  test_import_service.py           duplicate detection, correction/versioning, malformed files
  test_matching_core.py            pure reconciliation engine unit tests (32 tests)
  test_reconciliation_service.py   DB-integration: runs, corrections, history
  test_resolution_service.py       DB-integration: manual resolution guards + persistence
  test_web_workflow.py             end-to-end: full Flask app through its test client
```

The dependency direction is one-way: web depends on services, which
depend on matching/ingestion/models, which depend on nothing else in
the app. Nothing in matching/core.py or ingestion/* touches a database
or HTTP request - both are tested with nothing but plain Python values.

## Database design

- **source_files** - one row per uploaded file. content_hash (sha256 of
  the raw bytes) is unique (enforced at the database level, not just in
  application code) - nullable so that a file recognized as a duplicate can
  still be recorded (with status=DUPLICATE) without needing a synthetic
  hash value of its own. Tracks row counts (created / corrected / unchanged
  / failed) and overall status.
- **transactions** - the *current* view of one trade as seen by one
  system (LEDGER or STATEMENT), keyed on (source_system, natural_key)
  where natural_key is the trade_id / reference. This is the row the
  matching engine pairs up.
- **transaction_versions** - every value a transaction has ever held. A
  correction file that changes a monetary/timing field closes the current
  version (effective_to) and opens a new one, rather than overwriting
  history - this is what answers "what did this row used to say?".
- **reconciliation_runs** - one row per reconciliation pass, with
  per-status counts (matched / matched-within-tolerance / differs /
  unmatched-ledger / unmatched-statement / manually-resolved /
  cancelled-excluded).
- **reconciliation_results** - one row per pairing (or lack of one)
  produced by a run: which ledger transaction was paired with which
  statement transaction, or which side was left over, and the resulting
  status.
- **field_differences** - one row per field that disagreed on a matched
  pair: both values, the delta, and whether it was significant (outside
  tolerance) or not.
- **manual_resolutions** - deliberately keyed on Transaction ids, not on
  a ReconciliationResult (which only exists for one run and gets
  regenerated every time). ledger_transaction_id and
  statement_transaction_id are each unique (enforced at the database
  level): a transaction can be covered by at most one manual decision, ever.

## Migrations

src/models.py is the single source of truth for the schema.
scripts/generate_migration.py emits the current models' CREATE TABLE
and CREATE INDEX statements into a numbered file under migrations/, and
scripts/migrate.py applies whichever migration files haven't been applied
yet, tracked in a schema_migrations table - so "run migrations" is an
explicit, repeatable step rather than implicit create_all() magic. A
longer-lived project would use Alembic for proper incremental/versioned
migrations; that felt like more machinery than this project needs, since
the schema hasn't diverged from the models.

## Ingestion design

src/ingestion has no dependency on SQLAlchemy or Flask. A SourceParser
takes raw CSV text and returns a ParseResult (NormalizedRow list +
RowError list) - pure functions, unit tested with plain strings.

src/services/import_service.py is the DB-aware layer on top: it hashes
the file, checks for a duplicate, calls the parser, then upserts
Transaction/TransactionVersion rows.

**Adding a third source format** means: implement SourceParser (one
parse() method), give it an expected_columns tuple for auto-detection,
and register it with register_parser(). Nothing else changes - the import
service, the matching engine, and the UI are all format-agnostic; they only
ever see NormalizedRow. tests/test_parsers.py::TestThirdSourceExtensibility
is a complete worked example: a hypothetical pipe-delimited third exchange,
implemented and registered in about 30 lines, producing the same normalized
shape (BUY, not B) as the other two.

## Reconciliation algorithm

src/matching/core.py::run_matching is the whole algorithm, in one place,
independently testable without a browser, HTTP, or a database
(tests/test_matching_core.py, 32 tests):

1. **Cancelled transactions are excluded first**, on both sides, before
   anything else runs. They never appear in a result, never count toward
   any total, and can never be manually matched (enforced again at the
   resolution_service layer - cancelled transactions can't be manually
   resolved either).
2. **Manual decisions are applied next**, ahead of automatic matching,
   because a human's prior call always wins - and because manual decisions
   are how the system supports pairing two transactions with *different*
   IDs (the assignment's own C-9001 example: a statement-only row with no
   matching ledger ID at all).
3. **Everything left is auto-matched by natural_key.** Since Transaction
   enforces uniqueness on (source_system, natural_key) at the database
   level, at most one ledger row and one statement row can ever share a
   given key - so this pairing is always deterministic and one-to-one; there
   is never a choice between candidates to get wrong.
4. Each pair is compared field by field
   (instrument, side, quantity, price, gross_amount,
   transacted_at, state). A pair with **zero** differing fields is
   MATCHED. A pair with differing fields, none of which exceed their
   tolerance, is MATCHED_WITHIN_TOLERANCE. A pair with **any** field
   outside its tolerance is DIFFERS - and every differing field is
   reported with both values and the delta, whether or not it was
   significant, so a person can see the full picture, not just the verdict.
5. **Whatever's left over on one side only** is UNMATCHED_LEDGER or
   UNMATCHED_STATEMENT.
6. **Ambiguity is refused, not guessed at.** If the data ever contains two
   conflicting manual instructions about the same transaction - which
   resolution_service prevents from ever being persisted in the first
   place, but the engine checks independently as defense in depth - neither
   is honored; the transaction falls through to normal matched/unmatched
   handling instead of the engine arbitrarily picking one. Ledger-side and
   statement-side IDs are independent namespaces, so this check never
   cross-contaminates: a ledger ID and a statement ID that happen to share
   the same string are never treated as conflicting with each other.

## Tolerance decisions

Configured in src/config.py: AMOUNT_TOLERANCE = 0.01 (currency units),
TIME_TOLERANCE_SECONDS = 60. Both are inclusive at the boundary - a delta
*equal to* the tolerance is normal drift, not a problem - and both are
environment-overridable (RECON_AMOUNT_TOLERANCE,
RECON_TIME_TOLERANCE_SECONDS).

Applied per field: quantity has **zero** tolerance (there's no
rounding/fee rationale for a quantity to drift, unlike a price or amount);
price and gross_amount each use the amount tolerance; transacted_at
uses the time tolerance; instrument, side, and state require an exact
match (there's no meaningful "close enough" for an instrument symbol or
buy/sell direction).

## Duplicate handling

A file is a duplicate if its raw bytes produce a sha256 hash that's already
been imported - filename doesn't matter, so a renamed resend is still
caught. This is enforced at the database level via a unique index on
content_hash, not only in application logic. A file recognized as a
duplicate is recorded (status=DUPLICATE, pointing at the original file's
ID in error_summary) but its rows are never processed, so re-uploading
the same file any number of times never creates duplicate transactions.

**Known limitation**: duplicate detection is by file content, not by
current database state. If a file is imported, then a *later* file
corrects one of its rows, then the *original* file's exact bytes are
re-uploaded intending to revert that correction - the system will
(correctly, by its own definition) recognize it as a duplicate of the
*first* upload and skip it, leaving the correction in place rather than
reverting it. Reverting a correction requires uploading a file with the
corrected-back values that isn't byte-identical to a prior upload. This
wasn't a scenario the assignment describes, and handling it would mean
redesigning duplicate detection around current values rather than file
content - a meaningfully bigger and more complex model for a case that may
never come up.

## Correction / version handling

A file is not a special "correction file" that has to be labeled as such
by whoever uploads it - it's a normal import, and a *correction* is
something the system detects by diffing each row against the transaction's
current values. Any existing (source_system, natural_key) whose new
values differ from what's currently stored is a correction; the file as a
whole is flagged is_correction=True if at least one row was.

When a row is corrected, the *current* TransactionVersion is closed
(effective_to set to the new file's import time) and a new version is
opened holding the new values - nothing is overwritten or deleted. The
Transaction row's own columns always mirror the latest version, so
reconciliation automatically uses current values with no special-casing.
Re-sending a file where a row's values are unchanged is a no-op (counted as
unchanged, no new version).

## Manual resolution behavior

Two operations, both in src/services/resolution_service.py:

- **create_manual_match**: pair one ledger transaction with one statement
  transaction. Rejects: either transaction not existing, being on the wrong
  side (two ledger transactions, say), being CANCELLED, or already being
  covered by an earlier manual resolution.
- **create_manual_accept_unmatched**: confirm that a transaction
  genuinely has no counterpart. Same guards, minus the "wrong side" check
  (there's only one transaction involved).

Both guards are enforced twice: once in application code (with a clear
error message), and once at the database level via a unique constraint on
each transaction-id column - the second is a narrow-race-condition backstop
(two near-simultaneous requests both passing the application check before
either commits), surfaced through the same clean error rather than an
unhandled server error if it's ever what actually catches the conflict.

Resolutions are stored independently of any specific run, keyed on the
underlying transactions. The matching engine reloads every manual
resolution fresh each time it runs and applies it *before* auto-matching -
so a decision made today is automatically honored in every future run
without any extra step, which is exactly what the assignment asks for
("whatever they decide must still hold tomorrow"). Submitting a resolution
through the UI immediately triggers a new reconciliation run so the
decision is visibly reflected right away, rather than only taking effect
the next time someone happens to click "run reconciliation."

## The UI

Thin Flask routes (src/web/routes.py) over a read-model service
(src/services/view_service.py) that assembles what's already been
persisted into plain dicts for the templates - it computes nothing about
matching or differences itself; that stays entirely in
src/matching/core.py. Routes validate their inputs (empty/missing file,
unknown source system, non-UTF-8 file, empty resolver name, invalid
transaction id, unknown run/result id) and turn failures into a flashed
message and a redirect rather than a 500.

- **Dashboard** (/) - latest run's status counts as cards, full run
  history table, current import counts, and the "run reconciliation now"
  button.
- **Import** (/imports) - upload form + import history table.
- **Run results** (/runs/<id>) - filter pills for every status, a sort
  control, and a table. "Needs attention first" is the default sort because
  that's what someone opening this screen each morning actually wants to
  see without hunting for it.
- **Transaction detail** (/results/<id>) - our ledger and the
  counterparty's statement side by side; a field-by-field table with both
  values, the delta, the tolerance that applied, and a plain-language
  verdict; the manual resolution record if one exists; a resolution form if
  the result is eligible (unmatched-on-one-side only); and a
  correction-history table per side, shown only when a side actually has
  more than one version.
- **Manual resolution** (POST /results/<id>/resolve) - validates the
  result is actually eligible, requires a name, accepts an optional note,
  and either matches with a chosen candidate (drawn from the other side's
  still-unmatched rows in the same run) or accepts the row as genuinely
  unmatched. On success it immediately re-runs reconciliation and redirects
  into the new run filtered to "Manually resolved." On failure it flashes
  the service layer's error and returns to the same page with nothing
  written.

## Assumptions and decisions

- **Timestamps** are normalized to UTC and stored as naive datetimes.
  SQLite has no native timezone-aware datetime type, so rather than have
  SQLAlchemy silently strip timezone info in a way that's easy to get wrong
  later, the app is explicit: everything in the database is UTC by
  convention, and the one conversion point is in import_service.py.
- **A timestamp with no offset is assumed to already be UTC** - this
  matches the statement format in the assignment's own example
  (2025-07-01 09:15:00, no Z, no offset).
- **Unrecognized side/direction values are a hard error** for that row
  (reported, row skipped) - BUY/B/SELL/S are the only values in the
  spec, and anything else likely indicates real data corruption worth
  surfacing rather than guessing at.
- **Unrecognized status/state values are preserved verbatim**, not
  collapsed into a shared "unknown" bucket - a settlement status this app
  doesn't recognize isn't necessarily bad data (a third source will have
  its own vocabulary), and two different unrecognized statuses must never
  be able to compare as equal just because neither was recognized.
- **"Correction file" is an outcome, not a flag** the uploader sets - see
  "Correction / version handling" above.
- **Duplicate detection is content-based** (sha256 of the raw file), not
  filename-based - see "Duplicate handling" above for the one documented
  edge case this implies.
- **Money and quantities are Decimal**, never float, from the moment
  they're parsed out of a CSV cell, specifically so tolerance comparisons
  are exact rather than subject to binary floating-point rounding.
- **SQLite** over Postgres/MySQL: zero setup, matches "any database" in the
  brief, and nothing here needs concurrent writers.
- **No authentication.** The person's name is a free-text field on the
  resolution form (used for the audit trail - who resolved what, and
  when), not a login. Out of scope for a take-home.

## Sample data (data/sample/)

- ledger_2025-07.csv / statement_2025-07.csv - a normal day's matched
  files, covering: an exact match (T-1001), an in-tolerance amount rounding
  difference (T-1002, $0.01), an in-tolerance timing drift (T-1003, 30s),
  two ledger-only rows with no statement counterpart (T-1004, T-1010), a
  real price discrepancy (T-1005), a real timing discrepancy (T-1006), a
  cancelled trade appearing on both sides (T-1007), and a statement-only
  row with no ledger counterpart (C-9001, taken directly from the
  assignment's own example).
- ledger_2025-07_correction.csv - same batch resent with T-1008's
  quantity and gross_amount fixed; every other row identical.
- statement_2025-07_correction.csv - same idea from the statement side:
  T-1002's rounding fixed, T-1005's price corrected to match the ledger.
- ledger_2025-07-05_malformed.csv - five rows, three intentionally broken
  (unrecognized side value, non-numeric quantity, missing timestamp) to
  exercise partial-import / row-level error handling.

## What was intentionally left out

- **Pagination** on the results page - it renders every row for a run. Fine
  at this sample data's scale; a production system reconciling thousands of
  trades a day would want it.
- **Authentication** - out of scope for a take-home; the "who resolved
  this" trail is a free-text name field, not a real identity.
- **Non-CSV formats** - the assignment only asks for CSV.
- **Alembic-style incremental migrations** - see "Migrations" above for why
  a generated single migration file was the right amount of machinery here.
- **Reverting a correction by re-uploading a prior file's exact bytes** -
  see the documented limitation under "Duplicate handling."

## What would be built next

- A way to undo/edit a manual resolution (currently the only way to change
  one is to act on the underlying data differently - e.g. correct a value
  - since a transaction that's already resolved is deliberately locked from
  being resolved again).
- Pagination and a search box on the results page for larger datasets.
- A CSV/PDF export of a run's results, for sharing outside the app.
- Bulk actions (e.g. "accept all of these as unmatched") for mornings with
  many small unmatched rows.
