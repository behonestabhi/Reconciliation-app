"""
Application configuration.

Kept deliberately small: a single DATABASE_URL, overridable via environment
variable so tests and the running app can point at different SQLite files
(or, in principle, any SQLAlchemy-supported database).
"""
import os
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DB_PATH = os.path.join(BASE_DIR, "recon.db")

DATABASE_URL = os.environ.get("RECON_DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

# Tolerances used when comparing a ledger transaction against its matched
# statement transaction. Differences at or below these thresholds are
# considered normal clock/rounding drift and are not flagged.
# Documented further in README.md under "Assumptions".
AMOUNT_TOLERANCE = Decimal(os.environ.get("RECON_AMOUNT_TOLERANCE", "0.01"))  # currency units
TIME_TOLERANCE_SECONDS = int(os.environ.get("RECON_TIME_TOLERANCE_SECONDS", "60"))
