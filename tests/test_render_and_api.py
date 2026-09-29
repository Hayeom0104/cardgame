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
    """§1.3.7 two-panel pattern: situation (enemy+log) + turn (ally+hand)."""
    attachments = panels.render_battle_screen(
        [_ally(), _ally("아쿠엘")],
        [_enemy(1), _enemy(2, "방패병")],
        {1: {"state": "planned", "label": "강타"},
         2: {"state": "acted", "label": "행동 완료"}},
        resource=4, round_no=3,
    )
    assert len(attachments) == 2
    assert [a.filename for a in attachments] == ["deckout_situation.png",
                                                 "deckout_turn.png"]
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
    assert attachment.height > attachment.width, "모바일 지도는 세로형이어야 합니다"


def test_battle_images_are_split_into_two_messages(db, balance, version, user_id):
    """내 턴은 조작 메시지에, 전황은 별도 메시지에 붙는다."""
    from app.api import server
    # 분리기는 활성 런의 표면 세대/리비전을 delivery intent에 고정한다.
    db.execute(
        "INSERT INTO runs (user_id, world_id, state, content_version_id, "
        "logical_session_id, surface_generation, presentation_revision, "
        "map_seed, rng_seed, run_currency, created_at, updated_at, last_activity_at) "
        "VALUES (?, 'tutorial', 'battle', ?, 'split-test', 1, 2, 11, 12, 0, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (user_id, version))
    response = {
        "action": "edit", "content": "전투", "components": [],
        "attachments": [
            {"filename": "deckout_situation.png", "data_b64": "a", "content_type": "image/png"},
            {"filename": "deckout_turn.png", "data_b64": "b", "content_type": "image/png"},
        ],
    }
    result = server._split_battle_images(db, response, user_id)
    assert result["action"] == "multi_action"
    assert [child["attachments"][0]["filename"] for child in result["actions"]] == [
        "deckout_turn.png", "deckout_situation.png"]


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
    monkeypatch.setattr(settings, "allow_unauthenticated_local", True)
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


# =====================================================================
# 연동 가이드(2026-09-11) §4B — X-ARI-Minigame-Secret
# =====================================================================
def test_event_is_rejected_without_the_ingress_secret_when_one_is_configured(
        client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingress_secret", "shh-its-a-secret")
    response = client.post("/event", json={
        "type": "message", "user_id": 1, "command": "덱아웃", "args": [],
        "raw_content": "!덱아웃",
    })
    assert response.status_code == 401
    assert response.json()["action"] == "ignore"


def test_event_is_rejected_with_the_wrong_ingress_secret(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingress_secret", "shh-its-a-secret")
    response = client.post(
        "/event",
        json={"type": "message", "user_id": 1, "command": "덱아웃", "args": [],
              "raw_content": "!덱아웃"},
        headers={"X-ARI-Minigame-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_event_is_accepted_with_the_correct_ingress_secret(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingress_secret", "shh-its-a-secret")
    response = client.post(
        "/event",
        json={"type": "message", "user_id": 1, "command": "덱아웃", "args": [],
              "raw_content": "!덱아웃"},
        headers={"X-ARI-Minigame-Secret": "shh-its-a-secret"},
    )
    assert response.status_code == 200


def test_event_is_accepted_without_a_header_when_no_secret_is_configured(client):
    """개발/테스트 기본값 — `settings.ingress_secret`이 비어 있으면(운영
    배포 전 미설정 상태) 검증을 건너뛴다."""
    response = client.post("/event", json={
        "type": "message", "user_id": 1, "command": "덱아웃", "args": [],
        "raw_content": "!덱아웃",
    })
    assert response.status_code == 200


def test_shutdown_is_rejected_without_the_ingress_secret_when_one_is_configured(
        client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingress_secret", "shh-its-a-secret")
    response = client.post("/shutdown", json={"timeout": 30})
    assert response.status_code == 401


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


def test_an_event_s_username_and_avatar_url_land_on_the_account(client):
    """연동 가이드(2026-09-11) §7 — Central 프로필 API엔 표시 이름/아바타가
    없다. 이 둘의 유일한 출처는 `/event` 페이로드 자체이고, 서버가 이벤트마다
    계정에 적어 둬야 허브가 실제로 그걸 보여줄 수 있다."""
    from app.api import server
    from app.api.custom_id import to_base36

    client.post("/event", json={
        "type": "message", "user_id": 778, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
        "username": "디스코드유저", "avatar_url": "https://cdn.discordapp.com/a.png",
    })
    join_id = f"dko:hub:join:{to_base36(778)}"
    client.post("/event", json={
        "type": "interaction", "user_id": 778, "guild_id": 1, "channel_id": 2,
        "custom_id": join_id, "values": [],
        "username": "디스코드유저", "avatar_url": "https://cdn.discordapp.com/a.png",
    })
    account = server.state["db"].one(
        "SELECT display_name, avatar_url FROM accounts WHERE user_id = 778")
    assert account["display_name"] == "디스코드유저"
    assert account["avatar_url"] == "https://cdn.discordapp.com/a.png"


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
        self.initial_screen = kwargs
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
    assert entered["action"] == "redirect"
    assert not entered.get("components")
    thread_id = entered["thread_id"]
    refresh_id = server.state["central"].initial_screen["components"][0]["custom_id"]
    opened = client_with_central.post("/event", json={
        "type": "interaction", "user_id": 55510, "guild_id": 1,
        "channel_id": thread_id, "thread_id": thread_id,
        "custom_id": refresh_id, "values": [],
    }).json()
    assert opened["action"] == "edit"
    assert opened["attachments"], "스레드의 초기 지도 그림이 없습니다"
    node_buttons = [c for row in opened["components"] for c in row.get("components", [row])]
    node_id = node_buttons[0]["custom_id"]

    payload = {"type": "interaction", "user_id": 55510, "guild_id": 1,
               "channel_id": thread_id, "thread_id": thread_id,
               "custom_id": node_id, "values": [], "event_id": "evt-node-committed"}
    first = client_with_central.post("/event", json=payload).json()
    assert first["action"] == "multi_action"
    assert first["actions"][0]["action"] == "edit"
    assert first["actions"][1]["action"] == "post_channel_message"

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
        "type": "interaction", "user_id": 55510, "guild_id": 1, "channel_id": thread_id,
        "thread_id": thread_id, "custom_id": battle_buttons[0]["custom_id"],
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
    assert client.get("/healthz").status_code == 503


def test_the_service_accepts_again_after_the_shutdown_window(client, monkeypatch):
    """연동 가이드 §5 — Central이 재시작하며 보낸 `/shutdown` 뒤에 Deckout이
    살아 있으면 영구적인 드레인에 머물면 안 된다. 예전엔 Deckout을 따로
    재시작할 때까지 모든 명령이 무시됐다."""
    from app.api import server

    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    client.post("/shutdown", json={"timeout": 30})
    message = {"type": "message", "user_id": 1, "command": "덱아웃", "args": [],
               "raw_content": "!덱아웃"}
    assert client.post("/event", json=message).json()["action"] == "ignore"

    clock[0] += 31
    assert client.get("/healthz").status_code == 200
    assert client.post("/event", json=message).json()["action"] != "ignore"


def test_thread_resume_is_visible_and_rebuilds_images_without_advancing(client_with_central):
    from app.api import server
    client = client_with_central
    uid = 55520
    unknown = client.post('/event', json={
        'type': 'interaction', 'user_id': uid, 'guild_id': 1, 'channel_id': 5959,
        'custom_id': 'dko:hub:daily', 'values': [],
    })
    assert unknown.status_code == 200
    join_id = unknown.json()['components'][0]['components'][0]['custom_id']
    entered = client.post('/event', json={
        'type': 'interaction', 'user_id': uid, 'guild_id': 1, 'channel_id': 5959,
        'custom_id': join_id, 'values': [],
    }).json()
    thread_id = entered['thread_id']
    refresh_id = server.state['central'].initial_screen['components'][0]['custom_id']
    base = {'type': 'interaction', 'user_id': uid, 'guild_id': 1,
            'channel_id': thread_id, 'thread_id': thread_id, 'values': []}
    opened = client.post('/event', json={**base, 'custom_id': refresh_id}).json()
    assert opened['action'] == 'edit'
    assert opened['attachments']
    node = opened['components'][0]['components'][0]['custom_id']
    battle = client.post('/event', json={**base, 'custom_id': node}).json()
    assert battle['action'] == 'multi_action'
    db = server.state['db']
    before = dict(db.one('SELECT * FROM runs WHERE user_id = ?', (uid,)))
    # 현재 스레드에서의 명령은 같은 스레드로 redirect만 보내면 복구가 안 된다.
    restored = client.post('/event', json={
        **base, 'type': 'message', 'command': '덱아웃', 'args': [],
        'raw_content': '!덱아웃', 'event_id': 'thread-resume-command',
    }).json()
    replay = client.post('/event', json={
        **base, 'type': 'message', 'command': '덱아웃', 'args': [],
        'raw_content': '!덱아웃', 'event_id': 'thread-resume-command',
    }).json()
    assert replay['action'] == 'reply'
    assert len(replay['attachments']) == 2
    assert restored['action'] == 'reply'
    assert len(restored['attachments']) == 2
    assert restored['components']
    assert dict(db.one('SELECT * FROM runs WHERE user_id = ?', (uid,))) == before
    client.post('/event', json={
        'type': 'message_delivery_result', 'success': True, 'partial': False,
        'action': 'reply', 'request_id': restored['metadata']['request_id'],
        'thread_id': thread_id, 'channel_id': thread_id, 'message_id': 987654,
    })
    assert dict(db.one('SELECT * FROM runs WHERE user_id = ?', (uid,))) == before
    # 실패 안내도 본인의 스레드에서는 일반 메시지지만 타인의 조작은 거절한다.
    invalid = client.post('/event', json={**base, 'custom_id': 'dko:bad'}).json()
    assert invalid['action'] == 'reply'
    other = client.post('/event', json={**base, 'user_id': uid + 1,
                                        'custom_id': refresh_id}).json()
    assert other['action'] == 'reply_ephemeral'
    assert dict(db.one('SELECT * FROM runs WHERE user_id = ?', (uid,))) == before


# =====================================================================
# 전황은 전투 시작 때 그림 한 번, 이후엔 최근 행동 5줄 코드블록 로그 (오너 지시)
# =====================================================================
def _enter_first_battle(client, uid):
    from app.api import server

    unknown = client.post('/event', json={
        'type': 'interaction', 'user_id': uid, 'guild_id': 1, 'channel_id': 5959,
        'custom_id': 'dko:hub:daily', 'values': []}).json()
    join_id = unknown['components'][0]['components'][0]['custom_id']
    entered = client.post('/event', json={
        'type': 'interaction', 'user_id': uid, 'guild_id': 1, 'channel_id': 5959,
        'custom_id': join_id, 'values': []}).json()
    thread_id = entered['thread_id']
    refresh_id = server.state['central'].initial_screen['components'][0]['custom_id']
    base = {'type': 'interaction', 'user_id': uid, 'guild_id': 1,
            'channel_id': thread_id, 'thread_id': thread_id, 'values': []}
    opened = client.post('/event', json={**base, 'custom_id': refresh_id}).json()
    node = opened['components'][0]['components'][0]['custom_id']
    return base, client.post('/event', json={**base, 'custom_id': node}).json()


def _first_control(action):
    component = action['components'][0]['components'][0]
    values = [component['options'][0]['value']] if component.get('options') else []
    return component['custom_id'], values


def test_the_situation_image_is_posted_once_then_the_battle_is_logged_as_text(
        client_with_central):
    base, start = _enter_first_battle(client_with_central, 55530)
    assert start['action'] == 'multi_action'
    assert start['actions'][1]['attachments'][0]['filename'] == 'deckout_situation.png'

    screen = start['actions'][0]
    logs = []
    for _ in range(6):
        custom_id, values = _first_control(screen)
        reply = client_with_central.post('/event', json={
            **base, 'custom_id': custom_id, 'values': values}).json()
        if reply['action'] != 'multi_action':
            screen = reply
            continue
        screen = reply['actions'][0]
        follow = reply['actions'][1]
        assert follow['attachments'] == [], "전황 그림은 전투 시작 때만 올라가야 합니다"
        logs.append(follow['content'])
        if not screen.get('components'):
            break
    assert logs, "카드를 낸 뒤 행동 로그가 올라와야 합니다"
    for content in logs:
        assert content.startswith('```\n') and content.endswith('\n```')
        assert 1 <= len(content.strip('`').strip().splitlines()) <= 5


def test_a_click_that_changes_nothing_posts_no_log_message(client_with_central):
    """낡은 버튼 재클릭처럼 새 행동이 없으면 로그 메시지를 올리지 않는다."""
    base, start = _enter_first_battle(client_with_central, 55531)
    custom_id, values = _first_control(start['actions'][0])
    client_with_central.post('/event', json={**base, 'custom_id': custom_id,
                                             'values': values})
    stale = client_with_central.post('/event', json={**base, 'custom_id': custom_id,
                                                     'values': values}).json()
    assert stale['action'] != 'multi_action', stale
