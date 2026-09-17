"""Migrate and publish an additive update, preserving existing custom content.

Back up the production SQLite database before invoking this command.
python -m app.cli.upgrade_deck_refresh --db deckout.db
"""
import argparse
from app.db.connection import Database
from app.content.versioning import current_version_id, create_version, copy_version, publish
from app.content.deck_refresh import apply
from app.engine.loadouts import backfill


def main():
    parser = argparse.ArgumentParser(description="Publish the deck refresh without reseeding existing content")
    parser.add_argument("--db", default="deckout.db")
    args = parser.parse_args()
    db = Database(args.db)
    db.migrate()
    current = current_version_id(db)
    if current is None:
        from app.content.seed import seed_all
        version = seed_all(db)
    else:
        with db.tx():
            version = create_version(db, is_draft=True)
            copy_version(db, current, version)
            apply(db, version)
            publish(db, version)
    for row in db.query("SELECT user_id FROM accounts"):
        backfill(db, row["user_id"], version)
    print(f"Published content version {version}; existing runs retain their snapshots.")
    db.close()


if __name__ == "__main__":
    main()
