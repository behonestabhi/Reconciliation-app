"""
Apply any migrations under migrations/*.sql that have not yet been applied
to this database, tracking progress in a schema_migrations table.

Usage:
    python scripts/migrate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db import engine  # noqa: E402

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def ensure_tracking_table(conn):
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
    )


def applied_migrations(conn) -> set[str]:
    rows = conn.execute(text("SELECT name FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def main():
    migration_files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))

    with engine.begin() as conn:
        ensure_tracking_table(conn)
        already = applied_migrations(conn)

        pending = [f for f in migration_files if f not in already]
        if not pending:
            print("no pending migrations")
            return

        for filename in pending:
            path = os.path.join(MIGRATIONS_DIR, filename)
            with open(path) as f:
                sql = f.read()

            print(f"applying {filename} ...")
            for statement in _split_statements(sql):
                conn.execute(text(statement))
            conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:name)"), {"name": filename})
            print(f"  ok")

    print(f"applied {len(pending)} migration(s)")


def _split_statements(sql: str) -> list[str]:
    """Split on ';' at end of line. Good enough for the simple CREATE TABLE
    statements this project generates; not a general SQL parser."""
    # Strip full-line comments first so a leading "-- ..." line doesn't
    # cause the whole statement that follows it to be mistaken for a comment.
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    statements = [s.strip() for s in without_comments.split(";\n") if s.strip()]
    cleaned = []
    for s in statements:
        s = s.rstrip(";").strip()
        if s:
            cleaned.append(s)
    return cleaned


if __name__ == "__main__":
    main()
