"""Initialize a Deckout database: migrate, seed content, publish.

    python -m app.cli.bootstrap --db deckout.db
"""

from __future__ import annotations

import argparse

from app.content.seed import seed_all
from app.content.versioning import current_version_id
from app.db.connection import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the Deckout database")
    parser.add_argument("--db", default="deckout.db")
    parser.add_argument("--force", action="store_true",
                        help="seed another content version even if one is current")
    args = parser.parse_args()

    db = Database(args.db)
    version = db.migrate()
    print(f"schema v{version} applied to {args.db}")

    existing = current_version_id(db)
    if existing is not None and not args.force:
        print(f"content version {existing} is already current; use --force to add one")
        return 0

    new_version = seed_all(db)
    print(f"published content version {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
