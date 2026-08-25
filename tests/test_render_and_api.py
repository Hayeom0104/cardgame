"""§11 Pillow rendering and the §1.3.1 endpoints."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.central.client import MAX_PNG_DIMENSION
from app.render import panels, theme


# =====================================================================
# §11 rendering
# =====================================================================
def _ally(name="이그니스", hp=40, hp_max=65, block=0, alive=True):
    return {"name": name, "hp_current": hp, "hp_max": hp_max, "block": block,
            "is_alive": alive, "tier": 2,
            "statuses": [{"status_id": "화상", "stacks": 2}]}


def _enemy(unit_id=1, name="고블린", hp=30, hp_max=55, alive=True):
    return {"battle_unit_id": unit_id, "name": name, "hp_current": hp,
            "hp_max": hp_max, "block": 0, "is_alive": alive, "tier": 1}


def test_the_battle_screen_is_two_pngs_in_one_action():
    """§1.3.7 two-panel pattern: combatants (enemy+ally) + hand (cards/passives/resource)."""
    attachments = panels.render_battle_screen(
        [_ally(), _ally("아쿠엘")],
        [_enemy(1), _enemy(2, "방패병")],
        {1: {"state": "planned", "label": "강타"},
         2: {"state": "acted", "label": "행동 완료"}},
        resource=4, round_no=3,
    )
    assert len(attachments) == 2
    assert [a.filename for a in attachments] == ["deckout_combatants.png",
                                                 "deckout_hand.png"]
    for attachment in attachments:
        attachment.validate()          # §1.3.7 limits
        assert not attachment.data_b64.startswith("data:")
        assert base64.b64decode(attachment.data_b64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_panels_stay_within_the_png_limits():
    attachments = panels.render_battle_screen(
        [_ally()], [_enemy(i) for i in range(8)], {}, resource=5, round_no=1)
    for attachment in attachments:
        assert attachment.decoded_size <= 4 * 1024 * 1024
        assert 1 <= attachment.width <= MAX_PNG_DIMENSION
        assert 1 <= attachment.height <= MAX_PNG_DIMENSION


def test_missing_art_still_renders_a_silhouette_and_name():
    """§11 asset fallback — the game never fails to render."""
    attachment = panels.to_attachment(
        panels.render_ally_panel([_ally(name="이름만 있는 캐릭터")], resource=3,
                                 round_no=1),
        "fallback.png")
    attachment.validate()
    assert attachment.decoded_size > 0


def test_the_enemy_panel_renders_all_eight_enemies():
    """§2.6 — the cap is set by readability; the panel must fit the cap."""
    image = panels.render_enemy_panel([_enemy(i) for i in range(8)], {})
    assert image.size == tuple(theme.load().size("panel_size"))


def test_the_map_renders_as_one_image():
    nodes = [{"node_index": i, "depth": d, "node_type": "전투"}
             for d, row in enumerate([1, 2, 3], start=1)
             for i in range(row)]
    for index, node in enumerate(nodes):
        node["node_index"] = index
    edges = [(0, 1), (0, 2), (1, 3), (2, 4)]
    attachment = panels.render_map(nodes, edges, current_node_index=0,
                                   available={1, 2})
    attachment.validate()
    assert attachment.filename == "deckout_map.png"


def test_the_settlement_screen_shows_kept_and_lost():
    """§8.6.3/§11 — the player must see what was kept and what was lost."""
    report = {
        "inventory": {
            "kept": [{"equipment_def_id": "eq_수련검", "tier": 2}],
            "tiered_down": [{"equipment_def_id": "eq_수련갑", "tier": 1}],
            "lost": [{"stone_tier": 3, "tier": 3}],
            "destroyed_by_tier_down": [],
        },
        "rewards": {"coin": 2250, "carta": 150},
    }
    attachment = panels.render_settlement(report)
    attachment.validate()


# =====================================================================
# §1.3.1 endpoints
# =====================================================================
@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "central_api_key", "")

    with TestClient(server.app) as test_client:
        # Seed content so the handlers have a published version to resolve.
        from app.content.balance import Balance
        from app.content.seed import seed_all

        version = seed_all(server.state["db"])
        server.state["content_version_id"] = version
        server.state["balance"] = Balance(server.state["db"], version)
        yield test_client


def test_healthz_reports_status(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_an_unknown_user_is_prompted_to_register_before_an_account_exists(client):
    """계정이 없는 사용자의 첫 명령은 가입 버튼만 돌려준다 — 계정은 그
    버튼을 눌러야 비로소 생긴다 (오너 지시로 §4.6.5의 조용한 자동 생성을
    명시적 가입 단계로 바꿨다)."""
    from app.api import server

    response = client.post("/event", json={
        "type": "message", "user_id": 777, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "reply"
    # /event 는 컴포넌트를 액션 로우로 감싼다 — 한 단계 더 들어간다.
    buttons = [c for row in body["components"] for c in row.get("components", [row])]
    register_id = next(c["custom_id"] for c in buttons
                       if c["custom_id"].startswith("dko:hub:join:"))
    assert server.state["db"].one(
        "SELECT user_id FROM accounts WHERE user_id = 777") is None

    interaction = client.post("/event", json={
        "type": "interaction", "user_id": 777, "guild_id": 1, "channel_id": 2,
        "custom_id": register_id, "values": [],
    })
    assert interaction.status_code == 200
    account = server.state["db"].one("SELECT * FROM accounts WHERE user_id = 777")
    assert account is not None
    # §4.6.3 — exactly one 10-pull's worth of 카르타.
    assert account["carta"] == 1600


def test_registering_twice_does_not_duplicate_the_account(client):
    """가입 버튼을 두 번 눌러도(재전송 등) 계정은 하나만 남는다 — 계정
    생성 자체가 `create_account`의 UNIQUE(user_id) 위에서 멱등이다."""
    from app.api import server

    from app.api.custom_id import to_base36

    client.post("/event", json={
        "type": "message", "user_id": 888, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
    })
    payload = {"type": "interaction", "user_id": 888, "guild_id": 1, "channel_id": 2,
               "custom_id": f"dko:hub:join:{to_base36(888)}", "values": []}
    client.post("/event", json=payload)
    client.post("/event", json=payload)
    count = server.state["db"].one(
        "SELECT COUNT(*) AS n FROM accounts WHERE user_id = 888")["n"]
    assert count == 1


def test_a_delivery_result_never_reaches_the_command_handlers(client):
    response = client.post("/event", json={
        "type": "message_delivery_result", "request_id": "unknown",
        "action": "edit", "success": True, "partial": False,
    })
    assert response.json()["action"] == "ignore"


def test_a_duplicate_event_id_is_dropped(client):
    """No run exists yet for this event, so there's nothing to replay —
    still just `ignore`."""
    payload = {"type": "message", "user_id": 999, "command": "덱아웃", "args": [],
               "raw_content": "!덱아웃", "event_id": "evt-42"}
    client.post("/event", json=payload)
    second = client.post("/event", json=payload)
    assert second.json().get("duplicate") is True


class _FakeCentral:
    """Enough of Central to let a run leave `preparing` and open a thread."""

    def __init__(self):
        self._next = 900000

    def create_thread(self, **kwargs):
        self._next += 1
        return {"thread_id": self._next, "message_id": 11}

    def recreate_thread(self, **kwargs):
        return self.create_thread(**kwargs)

    def edit_message(self, **kwargs):
        return {"status": "edited"}

    def get_user(self, user_id):
        return {}


@pytest.fixture
def client_with_central(client, monkeypatch):
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "parent_channel_id", 5959)
    server.state["central"] = _FakeCentral()
    yield client
    server.state["central"] = None


def test_a_duplicate_event_after_a_committed_node_click_replays_the_live_screen(
        client_with_central):
    """R3 B-02 / §9 Mandatory Test B — commit a node choice, discard the
    first response, redeliver the same `event_id`, and the player must get
    back live controls for the committed state without a service restart.

    Before this fix: the node click committed (run state -> `battle`) but a
    redelivery of the same event just returned `{"action": "ignore"}` — the
    old map button was already stale/rejected, and `!덱아웃` only redirected
    to the thread without pushing the new battle screen. The player was
    stuck with no way to obtain a battle control short of a service restart.
    """
    from app.api import server

    join = client_with_central.post("/event", json={
        "type": "message", "user_id": 55510, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
    }).json()
    buttons = [c for row in join["components"] for c in row.get("components", [row])]
    join_id = next(c["custom_id"] for c in buttons
                  if c["custom_id"].startswith("dko:hub:join:"))
    entered = client_with_central.post("/event", json={
        "type": "interaction", "user_id": 55510, "guild_id": 1, "channel_id": 2,
        "custom_id": join_id, "values": [],
    }).json()
    node_buttons = [c for row in entered["components"] for c in row.get("components", [row])]
    node_id = node_buttons[0]["custom_id"]

    payload = {"type": "interaction", "user_id": 55510, "guild_id": 1, "channel_id": 2,
               "custom_id": node_id, "values": [], "event_id": "evt-node-committed"}
    first = client_with_central.post("/event", json=payload).json()
    assert first["action"] == "edit"

    run = server.state["db"].one("SELECT run_id, state FROM runs WHERE user_id = ?",
                                 (55510,))
    assert run["state"] == "battle", "노드 클릭이 실제로 커밋되지 않았습니다"

    # 응답만 유실됐다고 가정하고 같은 event_id로 재전송한다.
    second = client_with_central.post("/event", json=payload).json()

    assert second["action"] == "edit", (
        "재전송이 살아 있는 화면을 돌려주지 않았습니다 — 아직도 그냥 ignore 입니다")
    assert second.get("duplicate") is not True
    assert second["components"], "재전송 응답에 누를 수 있는 컨트롤이 없습니다"
    assert second["attachments"], "재전송 응답에 전투 그림이 없습니다"

    # 그 컨트롤이 진짜로 눌리는지까지 확인한다 — 서비스 재시작 없이.
    battle_buttons = [c for row in second["components"]
                      for c in row.get("components", [row])]
    reply = client_with_central.post("/event", json={
        "type": "interaction", "user_id": 55510, "guild_id": 1, "channel_id": 2,
        "custom_id": battle_buttons[0]["custom_id"],
        "values": [battle_buttons[0]["options"][0]["value"]]
                  if "options" in battle_buttons[0] else [],
        "event_id": "evt-after-replay",
    }).json()
    assert reply["action"] != "ignore"


def test_shutdown_returns_truthful_counts_and_stops_intake(client):
    response = client.post("/shutdown", json={"timeout": 30})
    body = response.json()
    assert body["status"] == "ready"
    assert isinstance(body["saved_games"], int)
    assert isinstance(body["refunded_users"], int)

    # §1.3.2 step 1 — stop accepting new commands/interactions.
    after = client.post("/event", json={
        "type": "message", "user_id": 1, "command": "덱아웃", "args": [],
        "raw_content": "!덱아웃"})
    assert after.json()["action"] == "ignore"
