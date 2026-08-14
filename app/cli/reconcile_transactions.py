"""§17.3 rule 8 — re-examine GRANT transactions parked at `OPERATOR_REQUIRED`.

An `OPERATOR_REQUIRED` transaction means Central's applied delta did not
match what Deckout expected. That can be a real partial grant, or it can be
Deckout misparsing a response it fully understood the amount of (this is
what happened with the `amount`/`value_after` schema mismatch — see
CHANGELOG). Either way this is deliberately NOT automatic: it does not run at
startup, and it never calls `currency_add` with a fresh amount — it only
re-issues each transaction's ORIGINAL idempotency key, so Central replays its
already-settled result rather than applying anything new.

    python -m app.cli.reconcile_transactions --db deckout.db            # all
    python -m app.cli.reconcile_transactions --db deckout.db --tx t_123 # one

Requires DECKOUT_API_KEY / DECKOUT_CENTRAL_URL — this talks to the real
Central Bot.
"""

from __future__ import annotations

import argparse

from app.central.client import CentralClient
from app.central.transactions import GRANT, OPERATOR_REQUIRED, reconcile_operator_required
from app.config import settings
from app.db.connection import Database
from app.engine.progression import local_handlers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile GRANT transactions parked at OPERATOR_REQUIRED")
    parser.add_argument("--db", default="deckout.db")
    parser.add_argument("--tx", default=None, help="reconcile only this tx_id")
    args = parser.parse_args()

    if not settings.central_api_key:
        print("DECKOUT_API_KEY is not set — refusing to talk to Central")
        return 1

    db = Database(args.db)
    before = db.query(
        "SELECT tx_id FROM purchase_transactions WHERE status = ? AND direction = ?",
        (OPERATOR_REQUIRED, GRANT))
    if not before:
        print("no OPERATOR_REQUIRED grants to reconcile")
        return 0

    central = CentralClient(settings.central_base_url, settings.central_api_key)
    results = reconcile_operator_required(db, central, tx_id=args.tx,
                                          handlers=local_handlers())

    resolved = [r for r in results if r.status != OPERATOR_REQUIRED]
    still_stuck = [r for r in results if r.status == OPERATOR_REQUIRED]
    for result in resolved:
        print(f"{result.tx_id}: resolved -> {result.status} "
             f"(applied {result.applied_delta})")
    for result in still_stuck:
        print(f"{result.tx_id}: STILL operator_required "
             f"(re-checked applied {result.applied_delta}) — needs a human look")

    print(f"\n{len(resolved)} resolved, {len(still_stuck)} still stuck, "
         f"{len(results)} examined")
    return 1 if still_stuck else 0


if __name__ == "__main__":
    raise SystemExit(main())
