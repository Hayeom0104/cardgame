"""R3 M-03 — delete terminal runs' private threads past their retention window.

`terminal_thread_retention_hours` (§16.8, doc default 24h, `config/09_런_운영.toml`)
was defined but nothing ever read it: every completed/abandoned/expired/
admin-terminated run's private thread stayed around forever, and
`CentralClient.delete_thread()` was never called from anywhere. This is
deliberately NOT run at service startup, matching `reconcile_transactions.py`'s
own reasoning — deleting a Discord thread is externally visible and worth an
operator choosing when it happens, run as a periodic job (cron) or by hand.

    python -m app.cli.cleanup_terminal_threads --db deckout.db

Requires DECKOUT_API_KEY / DECKOUT_CENTRAL_URL — this talks to the real
Central Bot.
"""

from __future__ import annotations

import argparse

from app.central.client import CentralClient
from app.config import settings
from app.content.balance import Balance
from app.content.versioning import current_version_id
from app.db.connection import Database
from app.engine import lifecycle as lc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete terminal runs' threads past terminal_thread_retention_hours")
    parser.add_argument("--db", default="deckout.db")
    args = parser.parse_args()

    if not settings.central_api_key:
        print("DECKOUT_API_KEY is not set — refusing to talk to Central")
        return 1

    db = Database(args.db)
    version_id = current_version_id(db)
    if version_id is None:
        print("no published content version — nothing to do")
        return 0
    balance = Balance(db, version_id)

    central = CentralClient(settings.central_base_url, settings.central_api_key)
    report = lc.cleanup_terminal_threads(db, balance, central)

    print(f"checked {report['checked']} thread(s) due for cleanup")
    for outcome, count in sorted(report["by_outcome"].items()):
        print(f"  {outcome}: {count}")
    if report["checked"] == 0:
        print("nothing was due")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
