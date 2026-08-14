"""§1.3.5 — 런의 비공개 스레드를 실제로 여는 곳.

## 왜 이 파일이 따로 있는가

준비 화면의 [6] SURFACE 단계(`screens.surface_request`)는 delivery intent를
기록하고 응답에 `thread_request` 를 담아 돌려준다. 그런데 그 요청을 실제
호출로 바꾸는 코드가 어디에도 없었다 — `CentralClient.create_thread` 는 정의만
되어 있고 아무도 부르지 않았다.

그 상태에서 벌어지는 일은 `handlers._prepare_and_materialize` 의 주석이 이미
경고하고 있던 바로 그것이다: **스레드 없는 런이 계정만 점유한다.**
`!덱아웃 시작` 은 런 행을 만들고, `runs.thread_id` 는 NULL 로 남고, 이후의
`!덱아웃` 은 매번 스레드를 다시 만들려 하지만 그 역시 아무 일도 하지 않는다.
§16.3의 "계정당 하나" 규칙 때문에 새 런도 시작할 수 없다.

## 응답을 실행하는 자리

핸들러는 "무엇이 필요한지" 만 응답 딕셔너리에 적고, 그것을 실제 호출로 바꾸는
일은 여기 한 곳에서 한다. 핸들러마다 중앙봇 클라이언트를 들고 다니지 않아도
되고, 스레드를 여는 방식이 바뀌어도 고칠 곳이 하나다.

## 결과를 기록하는 방식

서비스 주도 API는 스레드를 **동기적으로** 돌려주므로 §1.3.3의 콜백이 오지
않을 수 있다. 그래도 결과는 `delivery.handle_delivery_result` 를 통해
기록한다 — 멱등성 검사와 §16.6 staleness 판정이 그 안에 있고, 두 경로가 서로
다른 방식으로 바인딩을 쓰기 시작하면 어느 쪽이 맞는지 알 수 없게 된다.
"""

from __future__ import annotations

import logging

from app.central import delivery
from app.db.connection import Database

logger = logging.getLogger(__name__)

#: 중앙봇 응답에서 스레드/메시지 id 를 찾을 때 볼 키들.
#:
#: 연동 가이드의 정확한 응답 스키마는 이 저장소에 없다(§1.1 표에 경로만
#: 있다). 그래서 흔한 이름 몇 개를 순서대로 보고, 하나도 없으면 **조용히
#: 넘어가지 않고** 실제로 무슨 키가 왔는지 로그에 남긴다. 가이드를 확인하면
#: 고칠 곳은 이 상수 하나다.
THREAD_ID_KEYS = ("thread_id", "id", "channel_id")
MESSAGE_ID_KEYS = ("message_id", "canonical_message_id")


class SurfaceError(RuntimeError):
    """스레드를 열지 못했다. 런은 그대로 남고 다음 `!덱아웃` 이 다시 시도한다."""


def _first(payload: dict, keys: tuple[str, ...]):
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return None


def fulfil_thread_request(db: Database, central, response: dict, *,
                          parent_channel_id: int) -> dict:
    """응답에 `thread_request` 가 있으면 스레드를 만들고 런에 묶는다.

    내부 키(`thread_request`, `run_id`)는 응답에서 걷어낸다 — 중앙봇에게
    보내는 것은 화면이지 우리 배선이 아니다.

    실패해도 예외를 밖으로 던지지 않는다. 스레드를 못 열었다고 플레이어의
    요청 전체를 500으로 만들 이유가 없고, 런은 이미 저장되어 있어서 다음
    `!덱아웃` 이 같은 `surface_generation` 으로 다시 시도한다 (§16.8).
    """
    request = response.pop("thread_request", None)
    run_id = response.pop("run_id", None)
    if request is None:
        return response

    if central is None:
        logger.error("중앙봇 클라이언트가 없어 런 %s 의 스레드를 열지 못했습니다",
                     run_id)
        return response
    if not parent_channel_id:
        logger.error("DECKOUT_CHANNEL_ID 가 설정되지 않아 런 %s 의 스레드를 "
                     "열지 못했습니다", run_id)
        return response

    request_id = (response.get("metadata") or {}).get("request_id")
    try:
        result = central.create_thread(
            logical_session_id=request["logical_session_id"],
            surface_generation=int(request["surface_generation"]),
            parent_channel_id=parent_channel_id,
            owner_user_id=int(request["owner_user_id"]),
            thread_name=request["thread_name"],
            content=response.get("content", ""),
            components=response.get("components") or [],
        )
    except Exception:                                        # noqa: BLE001
        # 같은 `surface_generation` 으로 다시 부르는 것은 멱등하므로(§1.3.5),
        # 여기서 실패한 것을 되돌릴 필요가 없다.
        logger.exception("런 %s 의 스레드 생성이 실패했습니다", run_id)
        return response

    _bind(db, request_id, result, run_id=run_id,
          surface_generation=int(request["surface_generation"]))
    return response


def _bind(db: Database, request_id: str | None, result: dict, *,
          run_id: int | None, surface_generation: int) -> None:
    """동기 응답을 §1.3.3 콜백과 같은 모양으로 바꿔 기록한다."""
    thread_id = _first(result or {}, THREAD_ID_KEYS)
    if thread_id is None:
        logger.error(
            "스레드 생성 응답에서 thread_id 를 찾지 못했습니다. 받은 키: %s "
            "— app/central/surfaces.py 의 THREAD_ID_KEYS 를 연동 가이드에 "
            "맞춰 고쳐야 합니다", sorted((result or {}).keys()))
        return

    if request_id is None:
        # intent 없이 온 경우는 없어야 하지만, 있더라도 스레드는 묶어 둔다 —
        # 묶지 않으면 방금 만든 스레드를 영영 찾을 수 없다.
        logger.warning("request_id 없이 스레드를 만들었습니다 (run %s)", run_id)
        if run_id is not None:
            db.execute("UPDATE runs SET thread_id = ? WHERE run_id = ?",
                       (int(thread_id), run_id))
        return

    delivery.handle_delivery_result(db, {
        "request_id": request_id,
        "action": "create_thread",
        "success": True,
        "partial": False,
        "thread_id": int(thread_id),
        "message_id": _first(result or {}, MESSAGE_ID_KEYS),
        "channel_id": result.get("parent_channel_id"),
    })


def reopen_thread(db: Database, central, run_id: int, *,
                  parent_channel_id: int) -> int | None:
    """§16.8 — 스레드가 사라진 런의 화면을 새 세대로 다시 연다.

    `lifecycle.recreate_surface` 가 `surface_generation` 을 올리는 일만 하고
    실제 재생성을 부르지 않아서, 스레드가 지워진 런은 세대 번호만 계속
    오르고 화면은 영영 돌아오지 않았다.

    돌려주는 값은 새 thread_id 이며, 열지 못했으면 None 이다.
    """
    from app.engine import lifecycle as lc

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise SurfaceError(f"run {run_id} does not exist")

    generation = lc.recreate_surface(db, run_id)
    if central is None or not parent_channel_id:
        logger.error("런 %s 의 스레드를 다시 열 수 없습니다 "
                     "(중앙봇 클라이언트 또는 채널 설정 없음)", run_id)
        return None

    request_id = delivery.mint_request_id("rethread")
    delivery.record_intent(
        db, request_id=request_id, run_id=run_id, purpose="canonical",
        surface_generation=generation,
        presentation_revision=int(run["presentation_revision"]),
    )
    try:
        result = central.recreate_thread(
            logical_session_id=run["logical_session_id"],
            surface_generation=generation,
            parent_channel_id=parent_channel_id,
            owner_user_id=int(run["user_id"]),
            thread_name=f"덱아웃 - {run['user_id']}",
        )
    except Exception:                                        # noqa: BLE001
        logger.exception("런 %s 의 스레드 재생성이 실패했습니다", run_id)
        return None

    _bind(db, request_id, result, run_id=run_id, surface_generation=generation)
    row = db.one("SELECT thread_id FROM runs WHERE run_id = ?", (run_id,))
    return int(row["thread_id"]) if row and row["thread_id"] else None
