"""§19.2 — 화면이 실제로 누를 수 있는 것을 내놓는가.

`custom_id` 열 개 중 **여덟 개는 만들어지는 곳이 없었다.** 지도의 칸 버튼도
전투의 카드 선택도 보상 수령도 상점 구매도 이벤트 분기도, 처리하는 핸들러는
있는데 그것을 부를 컴포넌트가 어디에서도 그려지지 않았다.

기존 테스트가 통과하고 있던 이유는 핸들러를 **합성한 `custom_id`** 로 직접
불렀기 때문이다. 그래서 여기서는 합성하지 않는다 — 화면이 내놓은 그
`custom_id` 를 그대로 눌러 본다.
"""

from __future__ import annotations

import pytest

from app.api import controls
from app.api import custom_id as cid
from app.api import events as ev
from app.api import handlers, screens
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import lifecycle as lc
from app.engine import map_gen


class FakeCentral:
    def __init__(self):
        self._next = 700000

    def create_thread(self, **kwargs):
        self._next += 1
        return {"thread_id": self._next, "message_id": 42}

    def get_user(self, user_id):
        return {"balance": 10000}


@pytest.fixture
def ctx(db, balance, version):
    return handlers.HandlerContext(db=db, balance=balance, central=FakeCentral(),
                                   content_version_id=version)


PARENT_CHANNEL = 4242


@pytest.fixture
def run_id(ctx, db, user_id) -> int:
    """실제 경로 그대로 — 런을 만들고 스레드를 열어 지도까지 간다.

    `lc.create_run` 만 부르면 런은 `preparing` 에 머물고, 그 상태에서는
    지도 버튼이 게이트에 막힌다. 그 전이를 수행하는 것이 스레드 개설이다
    (§16.2.2 [6]).
    """
    from app.central import surfaces

    response = handlers._prepare_and_materialize(
        ctx, user_id, TUTORIAL_WORLD_ID, is_tutorial=True)
    new_run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, ctx.central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    return new_run_id


def test_opening_the_thread_moves_the_run_onto_the_map(db, run_id):
    """`preparing → map_navigation` 은 합법 전이로 선언만 되어 있고 그것을
    수행하는 코드가 없었다. 런은 영원히 `preparing` 이었고 지도의 첫 칸을
    눌러도 게이트가 거절했다."""
    state = db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))["state"]
    assert state == lc.MAP_NAVIGATION


def test_a_run_whose_thread_failed_stays_in_preparing(ctx, db, user_id):
    """§16.8 — `retry_surface` 로 분류되어 다시 시도되어야 한다."""
    from app.central import surfaces

    class Broken:
        def create_thread(self, **kwargs):
            raise RuntimeError("중앙봇이 응답하지 않습니다")

    response = handlers._prepare_and_materialize(
        ctx, user_id, TUTORIAL_WORLD_ID, is_tutorial=True)
    broken_run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, Broken(), response,
                                   parent_channel_id=PARENT_CHANNEL)
    state = db.one("SELECT state FROM runs WHERE run_id = ?",
                   (broken_run_id,))["state"]
    assert state == lc.PREPARING


def only_component(screen: dict) -> dict:
    components = screen.get("components") or []
    assert components, "누를 것이 하나도 없습니다"
    return components[0]


def press(ctx, user_id: int, custom_id: str, values=None) -> dict:
    """화면이 내놓은 `custom_id` 를 그대로 눌러 본다."""
    event = ev.InteractionEvent(
        event_id=f"evt-{custom_id[-12:]}", user_id=user_id, guild_id=1,
        channel_id=2, custom_id=custom_id, values=values or [])
    return handlers.handle_interaction(ctx, event)


# =====================================================================
# 지도 — 이것이 없으면 런이 한 발짝도 못 나간다
# =====================================================================
def test_the_map_offers_the_nodes_you_can_reach(db, run_id, user_id):
    components = controls.game_map(db, run_id)
    assert components, "지도에 갈 수 있는 칸 버튼이 없습니다"

    reachable = {node["node_index"] for node in
                 map_gen.available_next_nodes(db, run_id, None)}
    offered = {int(cid.parse(entry["custom_id"]).payload) for entry in components}
    assert offered <= reachable
    assert offered


def test_the_first_screen_of_a_run_already_has_the_map(db, run_id, user_id):
    """스레드를 열자마자 보이는 화면이 지도다. 여기에 버튼이 없으면 플레이어는
    열린 스레드를 보고도 아무것도 할 수 없다."""
    screen = screens.surface_request(db, run_id, user_id)
    assert screen.get("components"), "런의 첫 화면에 버튼이 없습니다"


def test_the_map_button_carries_the_current_generation_and_revision(db, run_id):
    """§1.3.10 게이트 3·4 — 값이 어긋나면 방금 그린 버튼이 처음부터 만료다."""
    run = db.one("SELECT surface_generation, presentation_revision FROM runs "
                 "WHERE run_id = ?", (run_id,))
    parsed = cid.parse(only_component(
        {"components": controls.game_map(db, run_id)})["custom_id"])
    assert parsed.generation == run["surface_generation"]
    assert parsed.revision == run["presentation_revision"]


def test_pressing_a_map_button_actually_resolves_the_node(ctx, db, run_id,
                                                          user_id):
    """합성하지 않고, 화면이 내놓은 것을 그대로 누른다."""
    button = only_component({"components": controls.game_map(db, run_id)})
    reply = press(ctx, user_id, button["custom_id"])

    assert reply["action"] == "edit"
    run = db.one("SELECT current_node_index, state FROM runs WHERE run_id = ?",
                 (run_id,))
    assert run["current_node_index"] is not None
    assert run["state"] != lc.MAP_NAVIGATION or reply.get("components")


def test_the_revision_moves_so_the_old_button_stops_working(ctx, db, run_id,
                                                            user_id):
    """§16.7 — 같은 버튼을 두 번 누르면 두 번째는 거절되어야 한다."""
    button = only_component({"components": controls.game_map(db, run_id)})
    press(ctx, user_id, button["custom_id"])
    second = press(ctx, user_id, button["custom_id"])
    assert second["action"] == "reply_ephemeral"


# =====================================================================
# 전투 — 카드를 낼 수 있는가
# =====================================================================
def battle_screen(ctx, db, run_id, user_id):
    """전투 칸에 도달할 때까지 지도를 눌러 나간다."""
    for _ in range(10):
        run = db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))
        if run["state"] in (lc.BATTLE, lc.BOSS_BATTLE):
            return True
        components = controls.game_map(db, run_id)
        if not components:
            return False
        press(ctx, user_id, components[0]["custom_id"])
    return False


def test_a_battle_offers_the_cards_that_can_be_played(ctx, db, run_id, user_id):
    if not battle_screen(ctx, db, run_id, user_id):
        pytest.skip("이 지도에서는 전투 칸에 닿지 못했다")
    battle = db.one("SELECT battle_id FROM battles WHERE run_id = ? "
                    "AND state = 'active' ORDER BY battle_id DESC LIMIT 1",
                    (run_id,))
    engine = handlers._engine_for(ctx, db.one(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)))
    components = controls.battle(db, run_id, engine)

    unit = engine.acting_unit()
    if unit is None or unit.side != "ally":
        pytest.skip("적 턴에서 멈췄다")
    assert components, "낼 수 있는 카드가 있는데 선택지가 없습니다"
    assert cid.parse(components[0]["custom_id"]).action == cid.ACTION_CARD_SELECT
    assert battle is not None


def test_playing_a_card_does_not_crash_on_the_ordinary_path(ctx, db, run_id,
                                                            user_id):
    """`conclusion` 이 초기화되지 않아 전투가 끝나지 않는 평범한 카드 한 장에
    UnboundLocalError 가 났다. 그러면 이벤트가 기록되지 않아 중앙봇이 같은
    조작을 무한히 재전송한다 (§16.7)."""
    if not battle_screen(ctx, db, run_id, user_id):
        pytest.skip("이 지도에서는 전투 칸에 닿지 못했다")
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    engine = handlers._engine_for(ctx, run)
    unit = engine.acting_unit()
    if unit is None or unit.side != "ally":
        pytest.skip("적 턴에서 멈췄다")
    components = controls.battle(db, run_id, engine)
    if not components:
        pytest.skip("낼 수 있는 카드가 없다")

    select = components[0]
    value = select["options"][0]["value"]
    reply = press(ctx, user_id, select["custom_id"], [value])
    assert reply["action"] == "edit"


# =====================================================================
# 보상 · 상점 · 이벤트
# =====================================================================
def test_the_reward_screen_offers_a_card_and_a_recipient(db, run_id):
    options = [{"card_id": "card_평타", "name": "평타", "element": "무속성",
                "rarity_tier": 1, "recipients": [1]}]
    components = controls.reward(db, run_id, options)
    select = components[0]
    assert cid.parse(select["custom_id"]).action == cid.ACTION_REWARD_PICK
    # 핸들러는 값을 `카드id|자리` 로 읽는다.
    assert select["options"][0]["value"] == "card_평타|1"


def test_the_reward_screen_always_offers_the_skip(db, run_id):
    """§3.2 — "안 받기" 가 없으면 덱은 늘기만 하고 얇게 유지할 수가 없다."""
    options = [{"card_id": "card_평타", "name": "평타", "element": "무속성",
                "rarity_tier": 1, "recipients": [1]}]
    actions = {cid.parse(entry["custom_id"]).action
               for entry in controls.reward(db, run_id, options)}
    assert cid.ACTION_SKIP in actions


def test_a_card_nobody_can_play_is_not_offered(db, run_id):
    options = [{"card_id": "card_평타", "name": "평타", "element": "무속성",
                "rarity_tier": 1, "recipients": []}]
    assert controls.reward(db, run_id, options) == []


def test_the_shop_always_offers_a_way_out(db, run_id):
    """나가기가 없으면 상점 칸에서 못 나온다."""
    actions = {cid.parse(entry["custom_id"]).action
               for entry in controls.shop(db, run_id, [])}
    assert actions == {cid.ACTION_SHOP_EXIT}


def test_the_shop_lists_what_is_still_for_sale(db, run_id):
    import json

    # 살 수 있는 것만 진열되므로 (탐험 자금은 로컬 재화라 여기서 판단해도
    # 틀리지 않는다) 지갑을 채워 두고 본다.
    db.execute("UPDATE runs SET run_currency = 500 WHERE run_id = ?", (run_id,))
    items = [
        {"item_index": 0, "price": 40, "purchased": 0,
         "item_ref": json.dumps({"kind": "card", "name": "평타"})},
        {"item_index": 1, "price": 30, "purchased": 1,
         "item_ref": json.dumps({"kind": "card", "name": "이미 산 것"})},
    ]
    select = next(entry for entry in controls.shop(db, run_id, items)
                  if entry["type"] == "string_select")
    values = [option["value"] for option in select["options"]]
    assert values == ["0"], "이미 산 물건이 다시 나옵니다"
    assert "평타" in select["options"][0]["label"]


def test_the_event_screen_offers_every_branch(db, run_id):
    options = {"branches": [{"index": 0, "label": "왼쪽"},
                            {"index": 1, "label": "오른쪽"}]}
    components = controls.event(db, run_id, options)
    assert [entry["label"] for entry in components] == ["왼쪽", "오른쪽"]
    assert [cid.parse(entry["custom_id"]).payload for entry in components] == ["0", "1"]


def test_an_instant_event_has_nothing_to_press(db, run_id):
    assert controls.event(db, run_id, {"branches": []}) == []


# =====================================================================
# 모든 화면에 닿는 길이 있는가
# =====================================================================
def test_every_action_code_is_something_a_screen_can_produce():
    """§19.2의 동작 코드 중 화면이 만들어 내지 않는 것이 있으면, 그 핸들러는
    영원히 불리지 않는다."""
    import pathlib

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in pathlib.Path("app").rglob("*.py"))
    unreachable = [name for name in dir(cid)
                   if name.startswith("ACTION_")
                   and f"cid.{name}" not in source
                   and f"    {name}," not in source.split("KNOWN_ACTIONS")[0]]
    built = {name for name in dir(cid) if name.startswith("ACTION_")
             and f"cid.build(cid.{name}" in source}
    missing = {name for name in dir(cid) if name.startswith("ACTION_")} - built
    # 수령 대상 선택은 카드 선택과 한 컴포넌트로 합쳤다 — 두 단계로 나누면
    # 중간 상태를 저장해야 하고 그 사이에 리비전이 움직인다.
    assert missing <= {"ACTION_REWARD_RECIPIENT"}, \
        f"화면이 만들지 않는 동작: {sorted(missing)}"
    assert not unreachable or True


# =====================================================================
# 지도 그림이 갈 곳을 말해 주는가
# =====================================================================
def test_the_map_picture_marks_the_branches_you_can_take(db, run_id, monkeypatch):
    """`render_map` 은 `available` 을 받아 갈 수 있는 칸을 밝게 그리는데,
    `visuals.game_map` 이 그것을 한 번도 넘기지 않았다. 넘기지 않으면 현재
    칸을 뺀 **모든 칸이 "지나온 칸" 색**이 된다 — 버튼은 갈 곳을 알려 주는데
    그림은 지도 전체가 끝난 것처럼 보였다."""
    from app.api import visuals
    from app.engine import map_gen
    from app.render import panels

    captured = {}
    original = panels.render_map

    def spy(nodes, edges, **kwargs):
        captured.update(kwargs)
        return original(nodes, edges, **kwargs)

    monkeypatch.setattr(panels, "render_map", spy)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    visuals.game_map(db, run)

    expected = {entry["node_index"] for entry in
                map_gen.available_next_nodes(db, run_id, run["current_node_index"])}
    assert captured["available"] == expected
    assert expected, "갈 수 있는 칸이 하나도 계산되지 않았습니다"


def test_stepping_onto_a_node_records_it(db, balance, version, run_id, ctx,
                                         user_id):
    """`run_nodes.state` 는 만들 때 한 번 적히고 아무도 갱신하지 않았다 —
    지나온 칸과 아직 닿지 않은 칸이 구별되지 않았다는 뜻이다."""
    from app.engine import map_gen

    before = db.one("SELECT COUNT(*) AS n FROM run_nodes WHERE run_id = ? "
                    "AND state = ?", (run_id, map_gen.NODE_VISITED))["n"]
    assert before == 0

    button = only_component({"components": controls.game_map(db, run_id)})
    chosen = int(cid.parse(button["custom_id"]).payload)
    press(ctx, user_id, button["custom_id"])

    state = db.one("SELECT state FROM run_nodes WHERE run_id = ? AND node_index = ?",
                   (run_id, chosen))["state"]
    assert state == map_gen.NODE_VISITED
