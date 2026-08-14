"""§10.4 `offer_reward` — 등록만 되어 있고 아무도 처리하지 않던 연산자.

이 연산자는 스펙도 있고 §10.5 검증도 통과하고 대시보드에서 이벤트에 넣을
수도 있었다. 그런데 그것이 만든 보류 선택을 처리하는 코드가 없어서, 저주받은
카드 제거용 화면이 그것을 대신 받아 **"제거할 저주받은 카드가 없습니다"** 라고
답하고는 보상을 조용히 없앴다.

`reward_tables` 는 스키마에 있는 테이블 중 유일하게 읽지도 쓰지도 않는
테이블이었다. 같은 구멍의 다른 쪽 끝이다.
"""

from __future__ import annotations

import json

import pytest

from app.api import controls
from app.api import custom_id as cid
from app.api import events as ev
from app.api import handlers
from app.content import validation
from app.content.operators import ValidationError
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import lifecycle as lc
from app.engine import nodes
from app.engine.rng import JournaledRng

TABLE_ID = "reward_원소무기고"
EVENT_ID = "event_원소무기고"


class FakeCentral:
    def __init__(self):
        self._next = 800000

    def create_thread(self, **kwargs):
        self._next += 1
        return {"thread_id": self._next, "message_id": 7}

    def get_user(self, user_id):
        return {"balance": 0}


@pytest.fixture
def ctx(db, balance, version):
    return handlers.HandlerContext(db=db, balance=balance, central=FakeCentral(),
                                   content_version_id=version)


@pytest.fixture
def run_id(db, balance, version, user_id) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )


def suspend_on_offer(db, run_id: int, table_id: str = TABLE_ID) -> dict:
    """`offer_reward` 로 멈춘 상태를 만든다 — §10.4.2가 쓰는 그 모양 그대로."""
    from app.db.connection import utcnow

    db.execute(
        "INSERT INTO pending_choices (choice_id, run_id, node_index, choice_type, "
        "options_json, remaining_operators_json, operator_cursor, status, "
        "rng_op_key, created_at) VALUES ('c1', ?, 0, 'offer_reward', ?, '[]', 0, "
        "'open', NULL, ?)",
        (run_id, json.dumps({"reward_table_id": table_id}), utcnow()))
    return db.one("SELECT * FROM pending_choices WHERE choice_id = 'c1'")


# =====================================================================
# 콘텐츠
# =====================================================================
def test_the_seed_actually_uses_the_operator(db, version):
    """쓰는 콘텐츠가 하나도 없으면 그 경로가 깨져 있어도 드러나지 않는다."""
    row = db.one("SELECT branches_json FROM events WHERE content_version_id = ? "
                 "AND event_id = ?", (version, EVENT_ID))
    assert row is not None
    operators = [effect["operator"]
                 for branch in json.loads(row["branches_json"])
                 for effect in branch.get("effects", [])]
    assert "offer_reward" in operators


def test_the_reward_table_exists_and_is_not_empty(db, version):
    row = db.one("SELECT entries_json FROM reward_tables WHERE content_version_id = ? "
                 "AND reward_table_id = ?", (version, TABLE_ID))
    assert row is not None
    assert json.loads(row["entries_json"])


# =====================================================================
# §10.5 — 저장할 때 막는다
# =====================================================================
def test_a_missing_reward_table_is_rejected(db, version):
    db.execute("DELETE FROM reward_tables WHERE content_version_id = ?", (version,))
    with pytest.raises(ValidationError, match="보상 목록"):
        validation.validate_version(db, version)


def test_a_reward_table_naming_an_unknown_card_is_rejected(db, version):
    db.execute(
        "UPDATE reward_tables SET entries_json = ? WHERE content_version_id = ? "
        "AND reward_table_id = ?",
        (json.dumps([{"card_id": "card_없는것", "weight": 1}]), version, TABLE_ID))
    with pytest.raises(ValidationError, match="없습니다"):
        validation.validate_version(db, version)


def test_a_zero_weight_is_rejected(db, version):
    db.execute(
        "UPDATE reward_tables SET entries_json = ? WHERE content_version_id = ? "
        "AND reward_table_id = ?",
        (json.dumps([{"card_id": "card_평타", "weight": 0}]), version, TABLE_ID))
    with pytest.raises(ValidationError, match="weight"):
        validation.validate_version(db, version)


def test_an_effect_after_the_offer_is_rejected(db, version):
    """수령이 끝나면 런은 지도로 돌아가므로 그 뒤의 효과는 실행되지 않는다."""
    branches = [{"label": "받는다", "effects": [
        {"operator": "offer_reward", "params": {"reward_table_id": TABLE_ID}},
        {"operator": "grant_currency",
         "params": {"currency": "run_currency", "amount": 10}},
    ]}]
    db.execute("UPDATE events SET branches_json = ? WHERE content_version_id = ? "
               "AND event_id = ?", (json.dumps(branches), version, EVENT_ID))
    with pytest.raises(ValidationError, match="뒤에 다른 효과"):
        validation.validate_version(db, version)


# =====================================================================
# 실제 동작
# =====================================================================
def test_the_suspension_becomes_a_real_card_offer(db, balance, version, run_id):
    choice = suspend_on_offer(db, run_id)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])

    result = nodes.materialize_offer_reward(db, balance, rng, run_id, choice)

    assert result["screen"] == "reward"
    assert result["options"], "고를 카드가 없습니다"
    for option in result["options"]:
        assert option["recipients"], "받을 자리가 없는 카드가 제안되었습니다"


def test_the_offer_becomes_the_same_shape_as_a_reward_node(db, balance, version,
                                                           run_id):
    """보상 칸과 같은 모양이어야 이미 있는 화면과 수령 코드가 처리한다."""
    choice = suspend_on_offer(db, run_id)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    nodes.materialize_offer_reward(
        db, balance, JournaledRng(db, run_id, run["rng_seed"]), run_id, choice)

    updated = db.one("SELECT choice_type FROM pending_choices WHERE choice_id = 'c1'")
    assert updated["choice_type"] == nodes.CHOICE_REWARD
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.REWARD_SELECTION


def test_only_one_choice_stays_open(db, balance, version, run_id):
    """`_open_choice` 는 열린 선택이 하나뿐이라고 가정한다."""
    choice = suspend_on_offer(db, run_id)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    nodes.materialize_offer_reward(
        db, balance, JournaledRng(db, run_id, run["rng_seed"]), run_id, choice)

    open_rows = db.query("SELECT choice_id FROM pending_choices WHERE run_id = ? "
                         "AND status = 'open'", (run_id,))
    assert len(open_rows) == 1


def test_the_offer_screen_has_buttons_that_work(db, balance, version, run_id,
                                                ctx, user_id):
    choice = suspend_on_offer(db, run_id)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    offer = nodes.materialize_offer_reward(
        db, balance, JournaledRng(db, run_id, run["rng_seed"]), run_id, choice)

    components = controls.reward(db, run_id, offer["options"])
    select = components[0]
    assert cid.parse(select["custom_id"]).action == cid.ACTION_REWARD_PICK

    before = db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                    (run_id,))["n"]
    event = ev.InteractionEvent(
        event_id="evt-offer", user_id=user_id, guild_id=1, channel_id=2,
        custom_id=select["custom_id"], values=[select["options"][0]["value"]])
    reply = handlers.handle_interaction(ctx, event)

    assert reply["action"] == "edit"
    after = db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                   (run_id,))["n"]
    assert after == before + 1, "카드를 받았다는데 덱이 그대로입니다"


def test_a_table_of_unplayable_cards_offers_nothing(db, balance, version, run_id):
    """아무도 낼 수 없는 카드만 있는 목록은 고를 수 없는 화면이 된다."""
    db.execute(
        "UPDATE reward_tables SET entries_json = ? WHERE content_version_id = ? "
        "AND reward_table_id = ?",
        (json.dumps([{"card_id": "card_광_각성", "weight": 1}]), version, TABLE_ID))
    choice = suspend_on_offer(db, run_id)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    result = nodes.materialize_offer_reward(
        db, balance, JournaledRng(db, run_id, run["rng_seed"]), run_id, choice)
    assert result["screen"] == "map"


def test_an_unknown_table_at_runtime_is_a_clear_error(db, balance, version,
                                                      run_id):
    choice = suspend_on_offer(db, run_id, table_id="reward_없는것")
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    with pytest.raises(nodes.NodeError, match="reward table"):
        nodes.materialize_offer_reward(
            db, balance, JournaledRng(db, run_id, run["rng_seed"]), run_id, choice)
