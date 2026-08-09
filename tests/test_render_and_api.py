"""§11 Pillow rendering and the §1.3.1 endpoints."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.central.client import MAX_PNG_DIMENSION
from app.render import panels


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
    """§1.3.7 two-panel pattern: ally panel + enemy panel."""
    attachments = panels.render_battle_screen(
        [_ally(), _ally("아쿠엘")],
        [_enemy(1), _enemy(2, "방패병")],
        {1: {"state": "planned", "label": "강타"},
         2: {"state": "acted", "label": "행동 완료"}},
        resource=4, round_no=3,
    )
    assert len(attachments) == 2
    assert [a.filename for a in attachments] == ["deckout_ally.png",
                                                 "deckout_enemy.png"]
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
    assert image.size == (panels.PANEL_WIDTH, panels.PANEL_HEIGHT)


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


def test_an_unknown_user_is_created_by_the_base_command(client):
    from app.api import server

    response = client.post("/event", json={
        "type": "message", "user_id": 777, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
    })
    assert response.status_code == 200
    assert response.json()["action"] == "reply"
    account = server.state["db"].one("SELECT * FROM accounts WHERE user_id = 777")
    assert account is not None
    # §4.6.3 — exactly one 10-pull's worth of 카르타.
    assert account["carta"] == 1600


def test_the_first_command_is_idempotent(client):
    from app.api import server

    payload = {"type": "message", "user_id": 888, "command": "덱아웃", "args": [],
               "raw_content": "!덱아웃"}
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
    payload = {"type": "message", "user_id": 999, "command": "덱아웃", "args": [],
               "raw_content": "!덱아웃", "event_id": "evt-42"}
    client.post("/event", json=payload)
    second = client.post("/event", json=payload)
    assert second.json().get("duplicate") is True


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
