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


@router.post("/event")
async def handle_event(request: Request, session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    payload = await request.json()
    event = EventRequest.parse(payload)

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


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": get_settings().bot_name}
