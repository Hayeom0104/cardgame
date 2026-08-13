"""중앙봇이 호출하는 `POST /event` 엔드포인트 (설계 문서 §1).

이 봇은 디스코드 게이트웨이에 붙지 않는다. 중앙봇이 지정 채널의 `!커맨드`
메시지를 여기로 보내고, 반환된 응답을 대신 게시한다.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.commands import dispatch
from ..core.protocol import BotResponse, EventRequest
from ..db.database import get_session

log = logging.getLogger(__name__)
router = APIRouter(tags=["central-bot"])
_accepting_events = True


def begin_accepting_events() -> None:
    """Reset the drain flag after a process/app lifespan starts."""
    global _accepting_events
    _accepting_events = True


@router.post("/event")
async def handle_event(request: Request, session: Session = Depends(get_session)) -> dict:
    global _accepting_events
    settings = get_settings()
    payload = await request.json()
    event = EventRequest.parse(payload)

    if event.type == "message_delivery_result":
        # This command-only migration has no durable message binding yet.  A
        # callback must nevertheless be acknowledged idempotently.
        return BotResponse.ignored().to_dict()
    if event.type == "thread_deleted":
        return BotResponse.ignored().to_dict()
    if not _accepting_events:
        return BotResponse.error("서비스가 종료 준비 중입니다.", ephemeral=event.type != "message").to_dict()
    if event.type in {"interaction", "modal_submit"}:
        return BotResponse.error(
            "이 버전은 아직 버튼 UI를 지원하지 않습니다. `!덱아웃` 명령을 사용해주세요.",
            ephemeral=True,
        ).to_dict()
    if event.type != "message":
        return BotResponse.error("지원하지 않는 이벤트 형식입니다.").to_dict()

    # 지정 채널 제한 (§1 "designated channels")
    if settings.allowed_channel_ids and event.channel_id not in settings.allowed_channel_ids:
        return BotResponse.ignored().to_dict()

    try:
        response = await dispatch(session, event, settings.command_prefix)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("이벤트 처리 실패: %s", payload)
        response = BotResponse.error("처리 중 오류가 발생했습니다.")

    return response.to_dict()


@router.post("/shutdown")
async def shutdown() -> dict:
    global _accepting_events
    _accepting_events = False
    return {"status": "ready", "saved_games": 0, "refunded_users": 0}


@router.get("/healthz")
@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": get_settings().bot_name}
