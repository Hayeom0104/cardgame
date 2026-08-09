"""Run the §10.5 validation pass over a content version.

The same pass every dashboard save runs (§10.3), so this is what CI should call
before a publish. Exits non-zero on the first violation — content is never
partially loaded.

    python -m app.cli.check_content --db deckout.db
"""

from __future__ import annotations

import argparse

from app.content.operators import ValidationError
from app.content.validation import validate_version
from app.content.versioning import current_version_id
from app.db.connection import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Deckout content")
    parser.add_argument("--db", default="deckout.db")
    parser.add_argument("--version", type=int, default=None,
                        help="content version id; defaults to the current one")
    args = parser.parse_args()

    db = Database(args.db)
    version = args.version or current_version_id(db)
    if version is None:
        print("no content version found")
        return 1

    try:
        validate_version(db, version)
    except ValidationError as error:
        print(f"content version {version} FAILED validation:\n  {error}")
        return 1
    print(f"content version {version} passed §10.5 validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
