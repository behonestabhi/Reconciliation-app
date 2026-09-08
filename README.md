Reconciliation App

A transaction reconciliation system that compares an internal trade ledger with a counterparty statement, identifies discrepancies, handles exceptions, and preserves human reconciliation decisions across future runs.

1. Overview

The Reconciliation App is a small web application built to solve a common financial operations problem: two independent systems record the same transactions, but their records may differ.

Differences can occur because of:

Different column names and data formats

Different date/time formats

Different transaction vocabulary, such as BUY vs B

Rounding or small fee differences

Clock differences between systems

Missing transactions on either side

Corrections to previously imported transactions

Duplicate file submissions

Transactions that require a human decision

The application provides a workflow for importing both files, reconciling the transactions, understanding discrepancies, and manually resolving exceptions.

The main goal is not simply to report that two files are different, but to make it clear what differs, by how much, why it matters, and what action is required.

2. Key Features

File ingestion and normalization

Imports CSV files from both systems.

Supports different column names and file formats.

Normalizes transaction direction values such as BUY / B and SELL / S.

Normalizes timestamps into a common representation.

Parses monetary values and quantities using Decimal.

Validates malformed rows and reports row-level errors.

Uses an extensible parser architecture so another source format can be added without changing the reconciliation engine.

Reconciliation

The system distinguishes between:

Matched — all compared fields agree.

Matched within tolerance — differences exist but are within configured tolerance.

Differs — at least one meaningful difference exceeds the configured tolerance.

Unmatched – Ledger — a ledger transaction has no statement counterpart.

Unmatched – Counterparty — a statement transaction has no ledger counterpart.

Manually resolved — a human has previously made a reconciliation decision.

Cancelled transactions are excluded from reconciliation because they are not intended to be compared.

Duplicate file handling

Files are identified using a SHA-256 hash of their raw contents rather than their filename.

This means:

Uploading the exact same file twice is detected.

Renaming a duplicate file does not bypass duplicate detection.

Duplicate files do not create duplicate transactions.

Correction and version history

If a later file contains a changed version of an existing transaction:

The current transaction is updated.

The previous value is retained in transaction history.

The system records the correction.

Reconciliation uses the latest effective version.

Users can inspect what the transaction previously contained.

Manual resolution

When automatic matching cannot determine a counterpart, a user can:

Manually pair two transactions.

Accept a transaction as genuinely unmatched.

Manual decisions are stored independently of a specific reconciliation run, meaning a decision made today continues to apply to future runs.

3. Technology Stack

Component

Technology

Language

Python 3.12

Web framework

Flask

ORM / Database layer

SQLAlchemy 2.0

Database

SQLite

Templates

Jinja / server-rendered HTML

Testing

pytest

API / frontend framework

None required

Migrations

Lightweight custom migration system

The application intentionally uses a simple stack suitable for a take-home assignment. SQLite provides a zero-configuration database while Flask and server-rendered templates keep the application easy to understand and review.

4. Architecture

The application follows a layered structure:

Browser
   │
   ▼
Flask Web Routes
   │
   ▼
Application Services
   │
   ├── Import Service
   ├── Reconciliation Service
   ├── Resolution Service
   └── View Service
   │
   ├───────────────┐
   ▼               ▼
Ingestion      Matching Engine
   │               │
   └───────┬───────┘
           ▼
        Database

A key architectural decision is that the reconciliation engine is independent of Flask and the database.

This makes the most important business logic easy to test using plain Python values.

5. Project Structure

recon/
│
├── app.py
├── requirements.txt
├── pytest.ini
├── README.md
├── .gitignore
│
├── data/
│   └── sample/
│       ├── ledger_2025-07.csv
│       ├── statement_2025-07.csv
│       ├── ledger_2025-07_correction.csv
│       ├── statement_2025-07_correction.csv
│       └── ledger_2025-07-05_malformed.csv
│
├── migrations/
│   └── 0001_initial.sql
│
├── scripts/
│   ├── generate_migration.py
│   └── migrate.py
│
├── src/
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   │
│   ├── ingestion/
│   │   ├── base.py
│   │   ├── normalize.py
│   │   ├── ledger_parser.py
│   │   ├── statement_parser.py
│   │   └── registry.py
│   │
│   ├── matching/
│   │   └── core.py
│   │
│   ├── services/
│   │   ├── import_service.py
│   │   ├── reconciliation_service.py
│   │   ├── resolution_service.py
│   │   └── view_service.py
│   │
│   └── web/
│       ├── routes.py
│       └── templates/
│           ├── base.html
│           ├── dashboard.html
│           ├── imports.html
│           ├── run_detail.html
│           └── result_detail.html
│
└── tests/
    ├── test_normalize.py
    ├── test_parsers.py
    ├── test_import_service.py
    ├── test_matching_core.py
    ├── test_reconciliation_service.py
    ├── test_resolution_service.py
    └── test_web_workflow.py

Dependency direction

The dependency direction is intentionally one-way:

Web
 ↓
Services
 ↓
Matching / Ingestion / Models

The matching and ingestion layers do not depend on HTTP requests or database sessions.

6. Reconciliation Workflow

The typical morning workflow is:

Import files
     ↓
Start reconciliation
     ↓
Normalize transactions
     ↓
Apply existing manual decisions
     ↓
Exclude cancelled transactions
     ↓
Automatically match transactions
     ↓
Compare matched transactions
     ↓
Apply tolerances
     ↓
Generate reconciliation results
     ↓
Review exceptions
     ↓
Manually resolve remaining items
     ↓
Run again when required

Example

Suppose the ledger contains:

T-1002 | BTC-USD | BUY | 0.50 | 62000.00

and the counterparty statement contains:

T-1002 | BTC-USD | B | 0.50 | 62000.01

The parser first normalizes B to BUY.

The amount difference is then evaluated against the configured tolerance.

If the difference is within tolerance, the result is reported as:

Matched within tolerance

rather than being treated as a failure.

This allows normal operational drift to be separated from genuine reconciliation problems.

7. Matching Algorithm

The matching algorithm is implemented in:

src/matching/core.py

The function run_matching contains the core reconciliation logic and can be tested without a database or browser.

Step 1 — Exclude cancelled transactions

Cancelled transactions are removed from the comparison process on both sides.

They:

Do not produce reconciliation results.

Do not contribute to reconciliation counts.

Cannot be manually reconciled.

Step 2 — Apply previous manual decisions

Previously stored manual decisions are applied before automatic matching.

This is important because a human decision should continue to be respected on future reconciliation runs.

It also allows the system to handle transactions whose IDs differ between systems.

Step 3 — Automatic matching

Remaining transactions are automatically matched using their normalized natural key, which corresponds to the transaction ID/reference for the respective source.

The database enforces uniqueness for:

(source_system, natural_key)

This provides deterministic one-to-one matching.

Step 4 — Field comparison

Matched transactions are compared field by field:

Instrument

Side

Quantity

Price

Gross amount

Timestamp

State

Step 5 — Tolerance evaluation

Each field is evaluated against its appropriate tolerance.

The result becomes:

MATCHED
MATCHED_WITHIN_TOLERANCE
DIFFERS

Step 6 — Remaining transactions

Anything not matched is classified as:

UNMATCHED_LEDGER
UNMATCHED_STATEMENT

The UI then allows a user to resolve those exceptions manually.

8. Tolerance Rules

The current defaults are:

AMOUNT_TOLERANCE = 0.01
TIME_TOLERANCE_SECONDS = 60

They can be overridden using environment variables:

RECON_AMOUNT_TOLERANCE
RECON_TIME_TOLERANCE_SECONDS

The tolerance boundary is inclusive.

For example, with a time tolerance of 60 seconds:

60 seconds difference → acceptable
61 seconds difference → discrepancy

Field-specific rules

Field

Tolerance

Quantity

None

Price

Amount tolerance

Gross amount

Amount tolerance

Timestamp

Time tolerance

Instrument

Exact

Side

Exact

State

Exact

Decimal is used for monetary values and quantities instead of binary floating-point numbers.

9. Duplicate File Handling

A file is considered a duplicate when its raw contents generate a SHA-256 hash that has already been imported.

Therefore:

ledger.csv
ledger-copy.csv
renamed-ledger.csv

will still be identified as the same file if their contents are identical.

The database also enforces the uniqueness constraint, providing protection beyond the application-level check.

A duplicate upload is recorded as a duplicate import, but its rows are not processed again.

Known limitation

Duplicate detection is intentionally content-based.

If an original file is uploaded, then a correction is uploaded, and later the exact original bytes are uploaded again, the original file will still be recognized as a duplicate.

Reverting a correction therefore requires a new file containing the desired values rather than the exact bytes of an earlier file.

This behavior is documented rather than hidden because changing the definition of duplicate detection would require a more complex data model.

10. Correction and Version History

A correction is treated as an outcome of importing a file rather than as a special file type.

When an existing transaction is imported with changed values:

Previous Version
       ↓
   correction
       ↓
Current Version

The previous version remains stored.

The application therefore supports both:

Current value — what reconciliation should use now.

Historical value — what the transaction previously contained.

Unchanged rows do not create unnecessary new versions.

This provides a basic audit trail without introducing unnecessary infrastructure.

11. Manual Resolution

Manual resolution is implemented in:

src/services/resolution_service.py

There are two supported operations.

Manual match

A user can pair:

Ledger transaction
        +
Statement transaction

The application validates that:

Both transactions exist.

They belong to different source systems.

Neither is cancelled.

Neither has already been manually resolved.

Accept as unmatched

A user can also confirm that a transaction genuinely has no counterpart.

The resolution records:

The transaction(s)

Resolver name

Timestamp

Optional note/reason

Manual resolutions are linked to the underlying transactions rather than to a single reconciliation run.

This means:

Run 1
  ↓
Transaction unmatched
  ↓
Human resolves it
  ↓
Run 2
  ↓
Same decision is still respected

12. Database Design

The primary tables are:

source_files

Stores uploaded file metadata, including:

Source system

Filename

SHA-256 content hash

Import status

Row counts

Correction information

transactions

Stores the current effective version of each transaction.

transaction_versions

Stores every historical version of a transaction.

This prevents corrections from destroying previous values.

reconciliation_runs

Stores each reconciliation execution and its summary counts.

reconciliation_results

Stores the result for each matched or unmatched transaction.

field_differences

Stores field-level discrepancies, including both values, deltas, and tolerance significance.

manual_resolutions

Stores human decisions independently from individual reconciliation runs.

13. User Interface

The application contains a simple server-rendered interface focused on the morning reconciliation workflow.

Dashboard

The dashboard provides:

Latest reconciliation run

Status counts

Run history

Import information

Start reconciliation action

Import page

Users can:

Upload ledger files

Upload statement files

View import history

See duplicate or failed imports

Results page

Users can:

Filter by reconciliation status

Sort results

Identify items requiring attention

Open individual reconciliation results

Transaction detail

The detail page shows:

Ledger transaction

Counterparty transaction

Field-by-field comparison

Both values

Difference/delta

Applied tolerance

Current resolution status

Correction history when available

Manual resolution

For eligible unmatched transactions, the user can:

Select a candidate transaction

Manually pair it

Accept it as genuinely unmatched

Provide their name

Add an optional note

14. Testing

The project uses pytest.

The test suite covers the most important business behavior rather than only testing HTTP routes.

Current test coverage includes:

Normalization

CSV parsing

Malformed input

Duplicate detection

Correction/version handling

Matching logic

Tolerance behavior

Cancelled transactions

Unmatched transactions

Manual resolution

Database integration

End-to-end Flask workflows

The reconciliation engine has dedicated pure-logic tests so its behavior can be validated without starting the web application.

Run the full suite with:

python -m pytest

For verbose output:

python -m pytest -v

To focus on the reconciliation engine:

python -m pytest tests/test_matching_core.py

15. Setup

Requirements

Python 3.12+

pip

No external database server is required.

Installation

Create and activate a virtual environment:

Windows

python -m venv .venv
.venv\Scripts\activate

macOS / Linux

python3 -m venv .venv
source .venv/bin/activate

Install dependencies:

pip install -r requirements.txt

16. Running the Application

From the project root:

python scripts/migrate.py

This creates/applies the database migrations.

Then start the application:

python app.py

Open:

http://127.0.0.1:5000/

17. Recommended Demo Workflow

The application can be demonstrated in approximately 3–5 minutes:

Open the dashboard.

Import the sample ledger and statement files.

Start a reconciliation run.

Show the summary counts.

Open an exact match.

Open a transaction with a tolerance-level difference.

Open a genuine discrepancy and show the field-level comparison.

Open an unmatched transaction.

Manually resolve it.

Run reconciliation again and show that the decision persists.

Import a correction file.

Show the correction/version history.

This demonstrates the application's main value without requiring a long walkthrough.

18. Sample Data

The sample files are located under:

data/sample/

They are designed to demonstrate the main scenarios required by the assignment.

ledger_2025-07.csv

Internal ledger transactions.

statement_2025-07.csv

Counterparty statement transactions.

Together they demonstrate:

Exact matches

Amount differences within tolerance

Timestamp differences within tolerance

Genuine discrepancies

Ledger-only transactions

Statement-only transactions

Cancelled transactions

Correction files

ledger_2025-07_correction.csv
statement_2025-07_correction.csv

These demonstrate correction and version-history behavior.

Malformed file

ledger_2025-07-05_malformed.csv

Contains intentionally invalid rows to demonstrate validation and row-level error handling.

19. Assumptions and Design Decisions

The assignment intentionally leaves some implementation decisions open. The following choices were made to keep the system reliable and understandable.

Timestamp handling

All timestamps are normalized to UTC.

SQLite does not provide a native timezone-aware datetime type, so the application stores normalized UTC values consistently.

A timestamp without an explicit offset is assumed to already represent UTC.

Money and quantities

Decimal is used rather than float to avoid binary floating-point rounding issues during reconciliation.

Status values

Known transaction directions such as BUY, B, SELL, and S are normalized.

Unknown direction values are rejected because guessing a transaction direction could produce an incorrect reconciliation.

Unknown state/status values are preserved rather than silently converted into a generic value.

Database

SQLite was selected because:

The assignment permits any database.

It requires no separate database server.

It keeps setup simple for reviewers.

The application does not require high-volume concurrent writes.

Authentication

Authentication is intentionally out of scope.

The resolver name is collected for the audit trail, but it is not intended to represent a secure authenticated identity.

20. What Was Intentionally Left Out

The application intentionally avoids features that would add complexity without materially improving the take-home submission.

Pagination

The results page currently displays all results for a run.

A production system handling thousands of transactions would use pagination and server-side filtering.

Authentication and authorization

Not included because authentication is outside the scope of the assignment.

Non-CSV formats

Only CSV is supported because that is the format required by the assignment.

Full migration framework

A lightweight migration mechanism is included instead of adding a larger migration dependency.

For a longer-lived production application, Alembic would be a natural next step.

Reverting a correction using an identical historical file

As described above, exact duplicate-file detection intentionally prevents an earlier identical file from being reprocessed.

21. Future Improvements

If this were developed beyond the take-home assignment, the next improvements would include:

Authentication and role-based authorization

Pagination and search for large reconciliation runs

CSV/PDF export of reconciliation reports

Bulk resolution actions

Ability to edit or undo manual resolutions

More sophisticated matching strategies for sources without shared transaction IDs

PostgreSQL for higher concurrency and production deployment

Alembic migrations

Background processing for large file imports

Audit/event logging for operational traceability

Monitoring and metrics

Additional source adapters for third-party formats

The current implementation deliberately focuses on correctness, explainability, testability, and a complete user workflow rather than attempting to build a full enterprise reconciliation platform.

22. Engineering Takeaways

The main engineering decisions in this project were driven by four principles:

Correctness over cleverness

The reconciliation engine uses deterministic matching and explicit tolerance rules instead of making uncertain guesses.

Explainability

A reconciliation result should tell the user not only that something is wrong, but also which field differs, what each system reported, and how large the difference is.

Auditability

Corrections and manual decisions are persisted so that the system can explain what happened over time.

Testability

The most important business logic is independent of Flask and the database, allowing it to be tested directly with fast unit tests.

Author

Abhi Chavan

GitHub: @behonestabhi
Email: jeetrc52@gmail.com

Repository: https://github.com/behonestabhi/Reconciliation-app

License

This project was created as a take-home software engineering assignment.
