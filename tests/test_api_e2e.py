"""`POST /event` 를 통한 엔드투엔드 테스트.

중앙봇이 보내는 요청을 흉내내서, 명령 파싱 → 게임 로직 → 이미지 렌더링까지
전 경로가 실제로 동작하는지 확인한다.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from cardgamebot.db.database import SessionLocal, engine, init_db
from cardgamebot.db.models import Base, UserCard, UserCharacter
from cardgamebot.db.seed import seed_all
from cardgamebot.main import app

USER_ID = "e2e-user"


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as s:
        seed_all(s)
    with TestClient(app) as c:
        yield c


def send(client: TestClient, content: str) -> dict:
    resp = client.post(
        "/event",
        json={
            "type": "message",
            "content": content,
            "user_id": USER_ID,
            "channel_id": "chan-1",
            "username": "테스터",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def text_of(payload: dict) -> str:
    return payload["response"]["content"]


def images_of(payload: dict) -> list[bytes]:
    return [base64.b64decode(f["data"]) for f in payload["response"]["files"]]


def grant(code: str, cards: list[str] | None = None) -> None:
    from cardgamebot.core.commands import get_or_create_user

    with SessionLocal() as s:
        user = get_or_create_user(s, USER_ID, "테스터")
        s.add(UserCharacter(user_id=user.id, character_code=code, star=3))
        for card in cards or []:
            s.add(UserCard(user_id=user.id, card_code=card))
        s.commit()


# ---------------------------------------------------------------------------


def test_non_command_messages_are_ignored(client):
    payload = send(client, "안녕하세요 그냥 잡담입니다")
    assert payload["handled"] is False


def test_unknown_subcommand_is_reported(client):
    assert "알 수 없는 명령" in text_of(send(client, "!카드 없는명령"))


def test_help_lists_commands(client):
    assert "명령어 목록" in text_of(send(client, "!카드 도움말"))


def test_profile_shows_currencies(client):
    body = text_of(send(client, "!카드 정보"))
    assert "카르타" in body and "카드 조각" in body and "와일드카드" in body


def test_daily_grants_carta_and_blocks_repeat(client):
    assert "출석 완료" in text_of(send(client, "!카드 출석"))
    assert "남았습니다" in text_of(send(client, "!카드 출석"))


def test_gacha_pull_returns_result_image(client):
    send(client, "!카드 출석")
    with SessionLocal() as s:
        from cardgamebot.core.commands import get_or_create_user

        user = get_or_create_user(s, USER_ID, "테스터")
        user.carta = 100_000
        s.commit()

    payload = send(client, "!카드 뽑기 standard 10")
    assert "가챠 결과" in text_of(payload)
    imgs = images_of(payload)
    assert len(imgs) == 1 and imgs[0].startswith(b"\x89PNG")


def test_start_run_renders_map_and_offers_nodes(client):
    grant("aria")
    payload = send(client, "!카드 시작 aria")
    body = text_of(payload)

    assert "새로운 런" in body
    assert "다음 목적지" in body
    imgs = images_of(payload)
    assert len(imgs) == 1 and imgs[0].startswith(b"\x89PNG")


def test_run_requires_owned_character(client):
    assert "보유하지 않은 캐릭터" in text_of(send(client, "!카드 시작 aria"))


def test_full_combat_flow_produces_battle_image(client):
    grant("aria", cards=["aria_pierce", "uni_sweep"])
    send(client, "!카드 시작 aria")

    # 1층은 항상 전투 노드다 (balance.FIRST_FLOOR_ALL_COMBAT).
    payload = send(client, "!카드 이동 1")
    body = text_of(payload)
    assert "전투" in body
    assert images_of(payload)[0].startswith(b"\x89PNG")

    # 전투가 끝날 때까지 1번 카드를 계속 낸다.
    # 자원 부족이나 드로우 더미 고갈(§2.2)이면 턴을 넘긴다.
    last = ""
    for _ in range(200):
        payload = send(client, "!카드 사용 1")
        body = text_of(payload)
        if body.startswith("❌"):
            payload = send(client, "!카드 넘기기")
            body = text_of(payload)
        last = body
        if "승리" in body or "패배" in body:
            break
    assert "승리" in last or "패배" in last, last


def test_map_command_requires_active_run(client):
    assert "진행 중인 런이 없습니다" in text_of(send(client, "!카드 맵"))


def test_abandon_run(client):
    grant("noel")
    send(client, "!카드 시작 noel")
    assert "포기했습니다" in text_of(send(client, "!카드 포기"))
    assert "진행 중인 런이 없습니다" in text_of(send(client, "!카드 맵"))


def test_research_list_and_unlock_flow(client):
    body = text_of(send(client, "!카드 연구"))
    assert "연구 시스템" in body and "party_2" in body

    # 재화가 없으면 해금이 거부된다.
    assert "부족" in text_of(send(client, "!카드 연구해금 party_2"))


def test_hub_shop_lists_equipment(client):
    body = text_of(send(client, "!카드 상점"))
    assert "허브 상점" in body


def test_health_endpoint(client):
    assert client.get("/health").json()["status"] == "ok"
