"""The FastAPI service — §1.3.1 required endpoints.

    POST /event     Mandatory (guide §6). All five inbound types (§1.2)
    POST /shutdown  Mandatory (guide §9). §1.3.2
    GET  /healthz   Optional ops convenience. The guide's /healthz is the
                    *Central Bot's*, not a minigame requirement

This service holds no Discord gateway connection and no bot token.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import events as ev
from app.api import handlers
from app.central import delivery
from app.central.client import CapabilityError, CentralClient
from app.config import settings
from app.content.balance import Balance
from app.content.versioning import current_version_id
from app.db.connection import Database
from app.engine import lifecycle

logger = logging.getLogger(__name__)

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.database_path)
    db.migrate()
    state["db"] = db

    version_id = current_version_id(db)
    state["content_version_id"] = version_id
    state["balance"] = Balance(db, version_id) if version_id else None

    if settings.central_api_key:
        client = CentralClient(settings.central_base_url, settings.central_api_key)
        state["central"] = client
        if not settings.skip_capability_check:
            # §1.3.4 — fail closed. The service refuses commands rather than
            # running under an assumed contract.
            client.check_capabilities()
    else:
        state["central"] = None
        if not settings.skip_capability_check:
            raise CapabilityError(
                "no Central API key configured; refusing to start (§1.3.4)"
            )

    # §16.8 startup scan; §17.4 transaction resume.
    if state["balance"] is not None:
        state["recovery_plan"] = lifecycle.recover_runs(db, state["balance"])
    state["accepting"] = True

    yield

    db.close()


app = FastAPI(title="Deckout", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok" if state.get("accepting") else "draining",
        "content_version_id": state.get("content_version_id"),
    }


@app.post("/event")
async def event(request: Request) -> JSONResponse:
    """All five inbound types. Parsed per type, never against a shared model."""
    payload = await request.json()

    if not state.get("accepting"):
        # §1.3.2 step 1 — stop accepting new commands/interactions.
        return JSONResponse({"action": "ignore"})

    try:
        parsed = ev.parse_event(payload)
    except ev.EventParseError as error:
        logger.warning("rejected inbound event: %s", error)
        return JSONResponse({"action": "ignore", "error": str(error)}, status_code=200)

    db: Database = state["db"]

    if isinstance(parsed, ev.DeliveryResultEvent):
        # Callback failure is advisory; never re-issue a delivery that may
        # already have succeeded.
        delivery.handle_delivery_result(db, payload)
        return JSONResponse({"action": "ignore"})

    if isinstance(parsed, ev.ThreadDeletedEvent):
        lifecycle.handle_thread_deleted(db, parsed.logical_session_id)
        return JSONResponse({"action": "ignore"})

    # §16.7 duplicate event protection.
    if parsed.event_id and lifecycle.event_already_handled(db, parsed.event_id):
        return JSONResponse({"action": "ignore", "duplicate": True})

    context = handlers.HandlerContext(
        db=db,
        balance=state.get("balance"),
        central=state.get("central"),
        content_version_id=state.get("content_version_id"),
    )
    if isinstance(parsed, ev.MessageEvent):
        response = handlers.handle_message(context, parsed)
    elif isinstance(parsed, ev.InteractionEvent):
        response = handlers.handle_interaction(context, parsed)
    else:
        response = handlers.handle_modal_submit(context, parsed)

    # Recorded only after the handler returns. Marking it up front would make a
    # handler exception permanently swallow Central's redelivery of an event
    # this service never actually processed.
    if parsed.event_id:
        lifecycle.record_event(db, parsed.event_id, None)
    return JSONResponse(response)


@app.post("/shutdown")
async def shutdown(request: Request) -> dict:
    """§1.3.2.

        1. Stop accepting new commands/interactions
        2. Durably persist every active run at its current state (§16)
        3. Resolve or park every non-terminal transaction (§17.4)
        4. Return truthful counts
    """
    body = await request.json() if await request.body() else {}
    timeout = int(body.get("timeout", 30))
    state["accepting"] = False

    db: Database = state["db"]
    saved = db.one(
        "SELECT COUNT(*) AS n FROM runs WHERE state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated')"
    )
    # Runs are persisted continuously — every mutation commits under the §16.7
    # CAS — so there is nothing to flush here, only to count truthfully.
    refunded = 0
    central = state.get("central")
    if central is not None:
        from app.central.transactions import resume_pending

        results = resume_pending(db, central)
        refunded = sum(1 for result in results
                       if result.status in ("compensated", "coin_refunded"))

    logger.info("shutdown requested (timeout=%ss)", timeout)
    return {"status": "ready", "saved_games": int(saved["n"]),
            "refunded_users": refunded}
