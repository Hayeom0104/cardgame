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
from app.central import delivery, surfaces
from app.central.client import CapabilityError, CentralClient, to_action_rows
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

    # 부모 채널이 없으면 스레드를 만들 수 없고, 스레드가 없으면 런은 화면
    # 없이 계정만 점유한다 (§16.3). 그 상태로 서비스를 여는 것보다 안 여는
    # 편이 낫다 — 플레이어가 먼저 알게 되는 것이 아니라 운영자가 먼저 알아야
    # 하는 문제다.
    if not settings.parent_channel_id and not settings.skip_capability_check:
        raise CapabilityError(
            "DECKOUT_CHANNEL_ID is not set; runs could not open their private "
            "thread and would occupy the account with no surface (§1.3.5)"
        )

    # §16.8 startup scan; §17.4 transaction resume.
    if state["balance"] is not None:
        plan = lifecycle.recover_runs(db, state["balance"])
        state["recovery_plan"] = plan
        # 계획을 세워 놓고 아무것도 하지 않으면 §16.3의 만료 규칙은 없는 것과
        # 같다. 만료된 런은 여기서 바로 정산한다 — 나머지 항목은 화면을 다시
        # 그리는 일이라 §16.6 단일 기록자 큐를 통해야 하므로 계획으로 남긴다.
        state["expired_settled"] = _settle_expired(
            db, state["balance"], plan, state.get("central"))
        # 계획을 세워 놓고 실행하지 않으면 그 계획은 없는 것과 같다. 아래 둘은
        # **플레이어가 스스로 풀 수 없다.**
        state["settlements_resumed"] = _resume_settlements(
            db, state["balance"], plan, state.get("central"))
        state["surfaces_retried"] = _retry_surfaces(
            db, plan, state.get("central"))
        # `rerender_*`/`resume_map` 도 마찬가지로 계획만 세워지고 아무도
        # 실행하지 않았다 — 로컬 상태는 전투로 넘어갔는데 디스코드 화면은
        # 지도에 멈춰 있는 채로 방치되는 사고가 바로 이 구멍이었다.
        state["stale_screens_rerendered"] = _rerender_stale_screens(
            db, state["balance"], plan, state.get("central"))
    if state["central"] is not None:
        from app.central.transactions import resume_pending
        from app.engine.progression import local_handlers

        # 시작 시에도 비종료 트랜잭션을 기록된 상태에서 재개한다 (§17.4).
        resume_pending(db, state["central"], local_handlers())
    state["accepting"] = True

    yield

    db.close()


def _settle_expired(db: Database, balance, plan: list[dict], central=None) -> int:
    """기동 시 이미 만료된 런을 정산한다 (§16.3).

    하나가 실패해도 나머지를 계속 처리한다. 만료 정산에 걸려 서비스가 아예
    뜨지 못하면, 막힌 계정을 풀어 줄 방법마저 사라진다.
    """
    settled = 0
    for entry in plan:
        if entry.get("action") != "settle_expired":
            continue
        run = db.one("SELECT * FROM runs WHERE run_id = ?", (entry["run_id"],))
        if run is None:
            continue
        try:
            report = lifecycle.expire_run(db, balance, run)
            settled += 1
        except Exception:                                    # noqa: BLE001
            logger.exception("run %s 만료 정산에 실패했습니다", entry["run_id"])
            continue
        # 서비스가 내려가 있는 동안 만료된 런이다. 플레이어는 여기 없고,
        # 스레드에는 지도가 그대로 남아 있다 (§1.3.6).
        kept = len(report.get("inventory", {}).get("kept", []))
        try:
            surfaces.close_run_surface(
                db, central, entry["run_id"],
                summary=f"오래 조작이 없어 이 런을 정리했습니다. "
                        f"보관 {kept}개. (§16.3)")
        except Exception:                                    # noqa: BLE001
            # 화면을 못 고쳤다고 정산을 되돌리지 않는다. 정산은 이미 끝났다.
            logger.exception("run %s 의 마지막 화면을 보내지 못했습니다",
                             entry["run_id"])
    if settled:
        logger.info("기동 시 만료된 런 %d개를 정산했습니다", settled)
    return settled


def _resume_settlements(db: Database, balance, plan: list[dict],
                        central=None) -> int:
    """정산 도중에 죽은 런을 이어서 끝낸다 (§16.2.1).

    `run_settlement` 은 **활성 상태다.** 거기 멈춰 있는 런은 §16.3의 계정당
    하나 규칙으로 계정을 붙들고 있는데, 정산 화면에는 누를 것이 없어서
    플레이어가 스스로 풀 방법이 없다 — 30분 만료를 기다리는 수밖에 없었다.

    `recover_runs` 는 처음부터 이 런들을 `resume_settlement` 로 분류하고
    있었다. 그 계획을 아무도 실행하지 않았을 뿐이다.

    정산 단계는 receipt(§17.6)와 저널 키(§16.4)로 멱등하므로, 어디서 멈췄든
    다시 들어가 이어서 끝낼 수 있다.
    """
    from app.central.transactions import GRANT, create_transaction, run_transaction
    from app.engine import settlement as sl
    from app.engine.rng import JournaledRng

    def grant_coin(user_id: int, amount: int, tx_id: str):
        if central is None:
            return None
        create_transaction(db, tx_id=tx_id, user_id=user_id,
                           operation="settlement", direction=GRANT,
                           expected_coin_delta=amount, local_required=False)
        return run_transaction(db, central, tx_id=tx_id, kind="settlement")

    resumed = 0
    for entry in plan:
        if entry.get("action") != "resume_settlement":
            continue
        run = db.one("SELECT * FROM runs WHERE run_id = ?", (entry["run_id"],))
        if run is None or run["state"] != lifecycle.RUN_SETTLEMENT:
            continue
        try:
            report = sl.advance_settlement(
                db, balance, JournaledRng(db, run["run_id"], run["rng_seed"]),
                run_id=run["run_id"],
                content_version_id=run["content_version_id"],
                grant_coin=grant_coin)
            resumed += 1
        except Exception:                                    # noqa: BLE001
            logger.exception("run %s 정산 재개에 실패했습니다", entry["run_id"])
            continue
        kept = len(report.get("inventory", {}).get("kept", []))
        try:
            surfaces.close_run_surface(
                db, central, entry["run_id"],
                summary=f"런을 마무리했습니다. 보관 {kept}개.")
        except Exception:                                    # noqa: BLE001
            logger.exception("run %s 의 마지막 화면을 보내지 못했습니다",
                             entry["run_id"])
    if resumed:
        logger.info("기동 시 정산이 멈춰 있던 런 %d개를 이어서 끝냈습니다", resumed)
    return resumed


def _retry_surfaces(db: Database, plan: list[dict], central=None) -> int:
    """스레드를 얻지 못한 채 준비 상태로 남은 런의 화면을 다시 연다 (§16.8).

    §16.8이 요구하는 대로 **같은 `surface_generation`** 으로 다시 만든다 —
    같은 세대의 생성은 멱등하므로, 실제로는 열렸는데 우리가 결과를 못 받은
    경우에도 스레드가 두 개 생기지 않는다.

    플레이어가 `!덱아웃` 을 치면 허브가 알아서 열어 주기는 한다. 다만 화면이
    없는 런이 계정을 붙들고 있다는 사실을 플레이어가 알 방법이 없으므로,
    기동할 때 우리가 먼저 푼다.
    """
    retried = 0
    for entry in plan:
        if entry.get("action") != "retry_surface":
            continue
        try:
            if surfaces.retry_surface(
                    db, central, entry["run_id"],
                    parent_channel_id=settings.parent_channel_id):
                retried += 1
        except Exception:                                    # noqa: BLE001
            logger.exception("run %s 의 스레드 재시도에 실패했습니다",
                             entry["run_id"])
    if retried:
        logger.info("기동 시 화면이 없던 런 %d개의 스레드를 열었습니다", retried)
    return retried


def _rerender_stale_screens(db: Database, balance, plan: list[dict],
                            central=None) -> int:
    """로컬 상태는 앞서 있는데 화면 전달이 실패한 런의 글자·버튼을 다시 맞춘다.

    `recover_runs` 는 처음부터 이 런들을 `resume_map`/`rerender_battle`/
    `rerender_pending_choice` 로 분류하고 있었다. 그 계획을 아무도 실행하지
    않아, 로컬 상태는 전투로 넘어갔는데 스레드에는 지도가 남아 있는 런이
    생겨도 재기동으로는 고쳐지지 않았다.

    `rerun_node_resolution`(노드 해결 도중)은 여기서 다루지 않는다 — 안전한
    재구성을 하려면 노드 해결을 다시 실행해야 하는데, 그 경계는 아직
    검증하지 못했다. 발명하는 대신 남겨 둔다.
    """
    rerendered = 0
    for entry in plan:
        if entry.get("action") not in ("resume_map", "rerender_battle",
                                       "rerender_pending_choice"):
            continue
        try:
            if surfaces.rerender_stale_screen(db, balance, central, entry["run_id"]):
                rerendered += 1
        except Exception:                                        # noqa: BLE001
            logger.exception("run %s 의 화면을 다시 맞추지 못했습니다",
                             entry["run_id"])
    if rerendered:
        logger.info("기동 시 화면이 밀려 있던 런 %d개를 다시 맞췄습니다", rerendered)
    return rerendered


app = FastAPI(title="Deckout", lifespan=lifespan)

# §10.1 관리자 대시보드. 중앙봇 계약(§1.3)과 완전히 분리된 경로이며,
# `DECKOUT_ADMIN_PASSWORD` 가 없으면 스스로 꺼진 상태로 응답한다.
from app.admin.routes import router as admin_router      # noqa: E402

app.include_router(admin_router)


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

    context = handlers.HandlerContext(
        db=db,
        balance=state.get("balance"),
        central=state.get("central"),
        content_version_id=state.get("content_version_id"),
    )

    # §16.7 duplicate event protection.
    if parsed.event_id and lifecycle.event_already_handled(db, parsed.event_id):
        # R3 B-02 — the mutation this event caused already committed on the
        # first delivery; only the response was lost. Answering with a bare
        # "ignore" left the player on a stale screen with no live controls
        # and no way back in short of a service restart. Answer with the
        # run's current authoritative screen instead, when it's one we can
        # safely rebuild from committed state alone.
        run_id = lifecycle.run_for_event(db, parsed.event_id)
        replay = handlers.current_screen(context, run_id) if run_id else None
        if replay is not None:
            if "components" in replay:
                replay["components"] = to_action_rows(replay["components"])
            return JSONResponse(replay)
        return JSONResponse({"action": "ignore", "duplicate": True})

    if isinstance(parsed, ev.MessageEvent):
        response = handlers.handle_message(context, parsed)
    elif isinstance(parsed, ev.InteractionEvent):
        response = handlers.handle_interaction(context, parsed)
    else:
        response = handlers.handle_modal_submit(context, parsed)

    # §1.3.5 — 핸들러는 "스레드가 필요하다"고 응답에 적기만 하고, 실제 호출은
    # 여기 한 곳에서 한다. 이 줄이 없으면 런은 스레드 없이 계정만 점유한다.
    response = surfaces.fulfil_thread_request(
        db, state.get("central"), response,
        parent_channel_id=settings.parent_channel_id)

    # Component Contract 2.0 — this HTTP response body IS the action Central
    # executes; it never passes through CentralClient. Handlers build a flat,
    # human-readable component list, so it has to be wrapped into action rows
    # with numeric types right here, at the very last step before it leaves.
    if "components" in response:
        response["components"] = to_action_rows(response["components"])

    # Recorded only after the handler returns. Marking it up front would make a
    # handler exception permanently swallow Central's redelivery of an event
    # this service never actually processed.
    if parsed.event_id:
        # R3 B-02 — recording `run_id=None` made a duplicate redelivery
        # unable to say which run's screen to replay (see the lookup above).
        run_id = lifecycle.most_relevant_run_id(db, parsed.user_id)
        lifecycle.record_event(db, parsed.event_id, run_id)
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
        from app.engine.progression import local_handlers

        # 핸들러 맵 없이 재개하면 코인 차감 뒤 크래시한 트랜잭션이
        # `apply_local=None`으로 완료 처리되어 로컬 효과가 영구 유실된다.
        results = resume_pending(db, central, local_handlers())
        refunded = sum(1 for result in results
                       if result.status in ("compensated", "coin_refunded"))

    logger.info("shutdown requested (timeout=%ss)", timeout)
    return {"status": "ready", "saved_games": int(saved["n"]),
            "refunded_users": refunded}
