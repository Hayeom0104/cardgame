"""§1.3.3 delivery intents and §16.6 durable update ordering.

The callback does **not** carry `surface_generation` or `presentation_revision`
(B-13). The Central Bot guide §8.7's payload is exactly `type`, `request_id`,
`action`, `success`, `partial`, `guild_id`, `channel_id`, `message_id`,
`thread_id`. v6.2 claimed those two fields were "persisted with the callback",
which is impossible.

The resolution: Deckout persists its own outbound delivery intent BEFORE
sending. `request_id` is minted here and is therefore always known in advance.
"""

from __future__ import annotations

import logging
import uuid

from app.db.connection import Database, utcnow

logger = logging.getLogger(__name__)


def mint_request_id(purpose: str) -> str:
    return f"dko-{purpose}-{uuid.uuid4().hex[:16]}"


def frame_request_id(run_id: int, generation: int, revision: int) -> str:
    """§16.6 — stable per revision, so a retry returns `already_applied`
    instead of double-editing."""
    return f"dko-frame-{run_id}-g{generation}-r{revision}"


def record_intent(db: Database, *, request_id: str, run_id: int | None,
                  purpose: str, surface_generation: int,
                  presentation_revision: int) -> None:
    """Step 1: BEFORE returning any delivery action that carries
    `metadata.request_id`, INSERT a delivery_intents row."""
    db.execute(
        "INSERT OR IGNORE INTO delivery_intents (request_id, run_id, purpose, "
        "surface_generation, presentation_revision, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (request_id, run_id, purpose, surface_generation, presentation_revision,
         utcnow()),
    )


def handle_delivery_result(db: Database, payload: dict) -> dict:
    """Step 3-5: MERGE the callback into the intent row.

    * Process **idempotently** — a repeated callback is a no-op.
    * Callback failure is **advisory**. Never re-issue a delivery that may
      already have succeeded (guide §8.7).
    * Reconciliation: a callback whose intent generation is older than the run's
      current `surface_generation` is RECORDED and IGNORED, never applied to a
      live binding.
    * A callback with an unknown `request_id` is logged and dropped.
    """
    request_id = payload.get("request_id")
    if not request_id:
        return {"handled": False, "reason": "no request_id"}

    intent = db.one("SELECT * FROM delivery_intents WHERE request_id = ?", (request_id,))
    if intent is None:
        logger.info("dropping delivery result for unknown request_id %s", request_id)
        return {"handled": False, "reason": "unknown request_id"}

    existing = db.one("SELECT applied FROM discord_bindings WHERE request_id = ?",
                      (request_id,))
    if existing is not None:
        return {"handled": True, "idempotent": True,
                "applied": bool(existing["applied"])}

    run = (db.one("SELECT * FROM runs WHERE run_id = ?", (intent["run_id"],))
           if intent["run_id"] else None)
    stale = bool(run and intent["surface_generation"] < run["surface_generation"])
    should_apply = bool(payload.get("success")) and not stale

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO discord_bindings (request_id, run_id, purpose, action, "
            "success, partial, surface_generation, presentation_revision, "
            "channel_id, message_id, thread_id, applied, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, intent["run_id"], intent["purpose"], payload.get("action"),
             int(bool(payload.get("success"))), int(bool(payload.get("partial"))),
             intent["surface_generation"], intent["presentation_revision"],
             payload.get("channel_id"), payload.get("message_id"),
             payload.get("thread_id"), int(should_apply), utcnow()),
        )
        if should_apply and run is not None:
            # This callback is the ONLY channel through which the service
            # learns a resulting message_id / thread_id.
            if payload.get("thread_id"):
                conn.execute("UPDATE runs SET thread_id = ? WHERE run_id = ?",
                             (payload["thread_id"], run["run_id"]))
            if payload.get("message_id") and intent["purpose"] == "canonical":
                conn.execute(
                    "UPDATE runs SET canonical_message_id = ? WHERE run_id = ?",
                    (payload["message_id"], run["run_id"]),
                )

    return {"handled": True, "idempotent": False, "applied": should_apply,
            "stale": stale}


# =====================================================================
# §16.6 single-writer queue
# =====================================================================
def enqueue_frame(db: Database, *, run_id: int, target_revision: int,
                  delivery_request_id: str, payload: dict) -> int | None:
    """One logical writer per run, keyed by logical_session_id.

    UNIQUE(delivery_request_id) guarantees `already_applied` on retry rather
    than a second edit.
    """
    import json

    try:
        cursor = db.execute(
            "INSERT INTO delivery_queue (run_id, target_revision, "
            "delivery_request_id, payload_json, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', 0, ?)",
            (run_id, target_revision, delivery_request_id,
             json.dumps(payload, ensure_ascii=False), utcnow()),
        )
        return int(cursor.lastrowid)
    except Exception as error:
        if "UNIQUE" in str(error):
            return None
        raise


def next_frame(db: Database, run_id: int):
    """Pop the next queued frame, discarding anything stale.

    A render job carries the revision it was built for; before sending, compare
    it to the run's current revision. A stale job is DISCARDED, not sent.
    """
    run = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        return None
    current = int(run["presentation_revision"])

    db.execute(
        "UPDATE delivery_queue SET status = 'discarded' WHERE run_id = ? "
        "AND status = 'queued' AND target_revision < ?",
        (run_id, current),
    )
    return db.one(
        "SELECT * FROM delivery_queue WHERE run_id = ? AND status = 'queued' "
        "ORDER BY target_revision, queue_id LIMIT 1",
        (run_id,),
    )


def mark_frame(db: Database, queue_id: int, status: str) -> None:
    db.execute(
        "UPDATE delivery_queue SET status = ?, attempts = attempts + 1 "
        "WHERE queue_id = ?", (status, queue_id),
    )
