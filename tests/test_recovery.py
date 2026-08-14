"""Central Bot 연동 계약 위반 네 가지와 그로 인해 막힌 복구 경로 하나.

실제 운영 중 신고된 문제였다: `!덱아웃 시작` 이 "처리 중 문제가 발생했습니다"
로 실패하고, 그 뒤로는 "이미 진행 중인 런이 있습니다" 로 계정이 막혔다. 코드를
확인해 보니 네 가지가 겹쳐 있었다.

    1. 컴포넌트를 평면 목록·문자열 타입으로 보냈다. Component Contract 2.0 은
       명시적 action row(`{"type": 1, ...}`)와 숫자 컴포넌트 타입을 요구한다.
    2. `/v1/currency/add` 응답을 `applied_delta`/`applied`/`balance` 로만
       읽었다. 실제 Central `ChangeResponse` 는 `amount`/`value_after` 를
       돌려준다 — 실제로 지급된 출석 코인 500이 0으로 읽혀 `operator_required`
       에 갇혔다.
    3. 전투 화면이 손패까지 세 장을 보냈다. 중앙봇은 action 하나에 PNG 두
       장까지만 받는다.
    4. 첨부에 `content_type` 이 없었다.

그리고 하나 더: `recover_runs()` 는 처음부터 "로컬 상태는 앞섰는데 화면
전달이 실패한 런" 을 `rerender_*`/`resume_map` 으로 분류하고 있었지만, 그
계획을 실행하는 코드가 없어서 계획으로만 남아 있었다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.api import controls
from app.api import events as ev
from app.api import handlers
from app.central import surfaces
from app.central import transactions as tx
from app.central.client import (CentralClient, CentralError, CurrencyResult,
                                to_action_rows)
from app.engine import lifecycle as lc

PARENT_CHANNEL = 5959
BATTLE_STATES = {lc.BATTLE, lc.BOSS_BATTLE}


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import server
    from app.config import settings
    from app.content.balance import Balance
    from app.content.seed import seed_all

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "recovery.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "central_api_key", "")

    with TestClient(server.app) as test_client:
        version = seed_all(server.state["db"])
        server.state["content_version_id"] = version
        server.state["balance"] = Balance(server.state["db"], version)
        yield test_client


# =====================================================================
# 1. Component Contract 2.0 — action rows, numeric types
# =====================================================================
def test_a_flat_button_list_becomes_a_single_row():
    flat = [{"type": "button", "custom_id": "a", "label": "가"},
           {"type": "button", "custom_id": "b", "label": "나"}]
    rows = to_action_rows(flat)
    assert rows == [{"type": 1, "components": [
        {"custom_id": "a", "label": "가", "type": 2, "style": 1},
        {"custom_id": "b", "label": "나", "type": 2, "style": 1},
    ]}]


def test_a_select_menu_gets_its_own_row_even_between_buttons():
    flat = [{"type": "button", "custom_id": "a", "label": "가"},
           {"type": "string_select", "custom_id": "s", "options": []},
           {"type": "button", "custom_id": "b", "label": "나"}]
    rows = to_action_rows(flat)
    assert [row["components"][0]["type"] for row in rows] == [2, 3, 2]
    assert len(rows) == 3


def test_more_than_five_buttons_split_across_rows():
    flat = [{"type": "button", "custom_id": str(i), "label": str(i)}
           for i in range(7)]
    rows = to_action_rows(flat)
    assert [len(row["components"]) for row in rows] == [5, 2]
    assert all(component["type"] == 2 for row in rows for component in row["components"])


def test_an_explicit_button_style_is_preserved():
    rows = to_action_rows([{"type": "button", "custom_id": "a", "label": "가",
                           "style": 4}])
    assert rows[0]["components"][0]["style"] == 4


def test_already_wrapped_rows_pass_through_unchanged():
    """멱등성 — 두 경계(직접 응답, CentralClient) 모두에 적용해도 안전해야 한다."""
    wrapped = [{"type": 1, "components": [{"type": 2, "style": 1,
                                          "custom_id": "a", "label": "가"}]}]
    assert to_action_rows(wrapped) == wrapped


def test_an_empty_list_stays_empty():
    assert to_action_rows([]) == []
    assert to_action_rows(None) == []


def test_an_unknown_component_type_is_rejected():
    with pytest.raises(CentralError, match="unknown component type"):
        to_action_rows([{"type": "text_input", "custom_id": "a"}])


def test_the_event_endpoint_wraps_components_before_they_leave(api_client):
    """`/event` 의 HTTP 응답 몸통이 곧 중앙봇이 실행할 action 이다 —
    `CentralClient` 를 거치지 않는 유일한 경계라 여기서 직접 확인한다.

    실제 신고된 사고는 이 경계에서 평면·문자열 타입 컴포넌트가 그대로
    나가면서 `!덱아웃 시작` 자체가 "처리 중 문제가 발생했습니다" 로
    거절된 것이었다."""
    response = api_client.post("/event", json={
        "type": "message", "user_id": 555, "guild_id": 1, "channel_id": 2,
        "command": "덱아웃", "args": [], "raw_content": "!덱아웃",
    })
    body = response.json()
    assert body.get("components"), "허브 화면에 누를 것이 없습니다"
    for row in body["components"]:
        assert row["type"] == 1
        for component in row["components"]:
            assert component["type"] in (2, 3)
            if component["type"] == 2:
                assert "style" in component


# =====================================================================
# 2. §1.3.8 currency response schema
# =====================================================================
def _client_with(handler) -> CentralClient:
    transport = httpx.MockTransport(handler)
    return CentralClient("http://central.example", "key",
                         client=httpx.Client(transport=transport))


def test_the_real_change_response_schema_is_parsed():
    """실제 배포된 Central 은 `amount`/`value_after` 를 돌려준다 —
    `applied_delta`/`applied`/`balance` 가 아니다. 이걸 읽지 못해 실제 +500
    출석 코인이 0으로 읽혔다."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["amount"] == 500
        return httpx.Response(200, json={"amount": 500, "value_after": 1500})

    result = _client_with(handler).currency_add(1, 500, "key1")
    assert result.applied == 500
    assert result.balance_after == 1500
    assert result.matched


def test_the_old_assumed_keys_still_work_as_a_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"applied_delta": 500, "balance": 1500})

    result = _client_with(handler).currency_add(1, 500, "key2")
    assert result.applied == 500
    assert result.balance_after == 1500


def test_the_real_schema_is_preferred_when_both_are_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"amount": 500, "applied": 999,
                                        "value_after": 1500, "balance": 999})

    result = _client_with(handler).currency_add(1, 500, "key3")
    assert result.applied == 500
    assert result.balance_after == 1500


def test_a_clamped_deduction_is_read_from_the_real_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["amount"] == -3000
        return httpx.Response(200, json={"amount": -500, "value_after": 0})

    result = _client_with(handler).currency_deduct(1, 3000, "key4")
    assert result.requested == -3000
    assert result.applied == -500
    assert not result.matched


def test_a_response_with_none_of_the_known_keys_reads_as_zero_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    result = _client_with(handler).currency_add(1, 500, "key5")
    assert result.applied == 0
    assert result.balance_after is None


def test_create_thread_and_edit_message_wrap_their_components():
    """§1.3.5/§1.3.6 로 나가는 두 호출도 같은 변환을 거쳐야 한다 —
    직접 JSON 응답이 아니라 `CentralClient` 를 통하는 다른 경계다."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"thread_id": 1, "message_id": 2})

    client = _client_with(handler)
    flat = [{"type": "button", "custom_id": "a", "label": "가"}]
    client.create_thread(logical_session_id="s", surface_generation=1,
                         parent_channel_id=1, owner_user_id=1, thread_name="t",
                         components=flat)
    assert seen["body"]["components"] == [{"type": 1, "components": [
        {"custom_id": "a", "label": "가", "type": 2, "style": 1}]}]


# =====================================================================
# 3/4. §1.3.7 attachment count and content_type
# =====================================================================
def test_the_battle_screen_stays_at_two_pngs_even_with_a_hand():
    from app.render import panels

    ally = [{"character_id": "c", "name": "아군", "hp_current": 10, "hp_max": 20,
            "statuses": []}]
    enemy = [{"battle_unit_id": 1, "enemy_id": "e", "name": "적",
             "hp_current": 5, "hp_max": 10}]
    hand = [{"card_id": "card_평타", "name": "평타", "cost": 1, "element": "물리",
            "rarity_tier": 1, "category": "공격"}]
    attachments = panels.render_battle_screen(ally, enemy, {}, resource=3,
                                              round_no=1, hand=hand)
    assert len(attachments) == 2
    for attachment in attachments:
        attachment.validate()


def test_attachments_declare_their_png_content_type():
    from app.api import visuals
    from app.render.panels import Attachment

    fake = Attachment(filename="a.png", data_b64="aGk=", width=1, height=1,
                      decoded_size=2)
    out = visuals.attach(fake)
    assert out == [{"filename": "a.png", "data_b64": "aGk=",
                   "content_type": "image/png"}]


# =====================================================================
# §17.3 rule 8 — OPERATOR_REQUIRED reconciliation
# =====================================================================
class ReplayingCentral:
    """실제 Central 의 멱등성을 흉내 낸다 — 같은 키는 저장된 결과를 재생한다."""

    def __init__(self):
        self.settled: dict[str, int] = {}
        self.add_calls: list[tuple[int, str]] = []

    def currency_add(self, user_id, amount, idempotency_key):
        self.add_calls.append((amount, idempotency_key))
        if idempotency_key in self.settled:
            return CurrencyResult(requested=amount, applied=self.settled[idempotency_key])
        self.settled[idempotency_key] = amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        raise AssertionError("a GRANT reconciliation must never deduct")


def test_a_grant_the_old_parser_misread_reconciles_without_a_new_grant(db, user_id):
    """이 케이스가 실제 사고였다: Central 은 500을 전부 지급했는데, 옛 파서가
    0으로 읽어 `operator_required` 에 갇혔다. 재조회는 **같은 키** 를 다시
    보내 저장된 결과를 재생할 뿐, 새 금액을 요청하지 않는다."""
    central = ReplayingCentral()
    key = tx.coin_key("run_clear", "opreq1")
    central.settled[key] = 500          # Central already fully applied it.

    tx.create_transaction(db, tx_id="opreq1", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=500,
                          local_required=False)
    tx._update(db, "opreq1", status=tx.OPERATOR_REQUIRED, central_status=tx.PARTIAL,
              coin_applied_delta=0)     # what the buggy parser had recorded

    results = tx.reconcile_operator_required(db, central)
    assert len(results) == 1
    assert results[0].status == tx.COMPLETED
    assert results[0].applied_delta == 500
    # Exactly one currency_add call, with the ORIGINAL key — never a fresh one.
    assert central.add_calls == [(500, key)]


def test_a_genuine_partial_grant_stays_parked(db, user_id):
    """재조회해도 여전히 안 맞으면 진짜 이상 현상이다 — 자동으로 풀지 않는다."""
    central = ReplayingCentral()
    key = tx.coin_key("run_clear", "opreq2")
    central.settled[key] = 250          # genuinely only half was applied

    tx.create_transaction(db, tx_id="opreq2", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=500,
                          local_required=False)
    tx._update(db, "opreq2", status=tx.OPERATOR_REQUIRED, central_status=tx.PARTIAL,
              coin_applied_delta=250)

    results = tx.reconcile_operator_required(db, central)
    assert results[0].status == tx.OPERATOR_REQUIRED
    assert results[0].applied_delta == 250


def test_reconciliation_never_touches_deduct_transactions(db, user_id):
    """§17.3 rule 8 은 GRANT 전용이다 — DEDUCT 쪽 이상은 COMPENSATION 경로다."""
    central = ReplayingCentral()
    tx.create_transaction(db, tx_id="ddct1", user_id=user_id, operation="hub_purchase",
                          direction=tx.DEDUCT, expected_coin_delta=-500)
    tx._update(db, "ddct1", status=tx.OPERATOR_REQUIRED)
    results = tx.reconcile_operator_required(db, central)
    assert results == []


def test_resume_pending_still_ignores_operator_required(db, user_id):
    """자동 시작 스캔은 여전히 이 상태를 건드리지 않는다 — 사람이 트리거해야
    한다."""
    central = ReplayingCentral()
    tx.create_transaction(db, tx_id="opreq3", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=500,
                          local_required=False)
    tx._update(db, "opreq3", status=tx.OPERATOR_REQUIRED)
    assert tx.resume_pending(db, central) == []


# =====================================================================
# §16.8 — stale screen recovery (state advanced, delivery failed)
# =====================================================================
class DeliveryCapturingCentral:
    def __init__(self):
        self._next_thread = 700000
        self.edits: list[dict] = []

    def create_thread(self, **kwargs):
        self._next_thread += 1
        return {"thread_id": self._next_thread, "message_id": 1}

    def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        return {"status": "edited"}


def _start_tutorial_run(db, balance, version, user_id) -> tuple[int, DeliveryCapturingCentral]:
    central = DeliveryCapturingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)

    def send(*args):
        event = ev.MessageEvent(event_id=f"cmd-{args}", user_id=user_id, guild_id=1,
                                channel_id=2, command="덱아웃", args=list(args),
                                raw_content="!덱아웃 " + " ".join(args))
        response = handlers.handle_message(ctx, event)
        return surfaces.fulfil_thread_request(
            db, central, response, parent_channel_id=PARENT_CHANNEL)

    send()
    send("시작")
    return db.one("SELECT run_id FROM runs WHERE user_id = ? ORDER BY run_id DESC "
                 "LIMIT 1", (user_id,))["run_id"], central


def test_a_map_screen_left_stale_is_pushed_with_working_buttons(db, balance,
                                                                 version, user_id):
    run_id, central = _start_tutorial_run(db, balance, version, user_id)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == lc.MAP_NAVIGATION

    # push_frame 은 성공한 뒤에야 리비전을 올리므로(§16.7 CAS), 지금 이 순간의
    # 컴포넌트를 먼저 읽어 둔다 — rerender 뒤에 다시 읽으면 이미 한 칸 앞선
    # 리비전이라 custom_id 가 달라진다.
    expected_ids = {component["custom_id"] for component in
                    controls.game_map(db, run_id)}

    assert surfaces.rerender_stale_screen(db, balance, central, run_id)
    assert len(central.edits) == 1
    pushed = central.edits[0]
    assert pushed["components"]
    pushed_ids = {component["custom_id"] for component in pushed["components"]}
    assert pushed_ids == expected_ids


def test_a_battle_screen_left_stale_is_pushed_with_the_live_hand(db, balance,
                                                                 version, user_id):
    run_id, central = _start_tutorial_run(db, balance, version, user_id)
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)

    # 지도의 첫 칸을 눌러 전투(또는 그 밖의 노드)로 들어간다.
    node_button = controls.game_map(db, run_id)[0]
    event = ev.InteractionEvent(event_id="press1", user_id=user_id, guild_id=1,
                                channel_id=2, custom_id=node_button["custom_id"],
                                values=[])
    handlers.handle_interaction(ctx, event)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run["state"] not in BATTLE_STATES:
        pytest.skip("이 시드에서 첫 칸이 전투가 아니었습니다")

    assert surfaces.rerender_stale_screen(db, balance, central, run_id)
    pushed = central.edits[-1]
    assert pushed["components"], "전투 화면인데 낼 카드가 재구성되지 않았습니다"


def test_a_run_with_nothing_committed_is_left_alone(db, balance, version, user_id):
    """복구할 것이 없으면 아무것도 보내지 않는다 — 빈 화면을 밀어 넣지 않는다."""
    from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                          party_character_ids=[STARTER_CHARACTER_ID],
                          is_tutorial=True),
        version)
    central = DeliveryCapturingCentral()
    # PREPARING, no thread yet — nothing to rerender.
    assert not surfaces.rerender_stale_screen(db, balance, central, run_id)
    assert central.edits == []
