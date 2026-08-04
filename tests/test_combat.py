"""전투 엔진 테스트 — 설계 문서 §2 의 확정 규칙을 검증한다."""

from __future__ import annotations

import pytest

from cardgamebot.game.combat import BattlePhase, CombatEngine, CombatError
from cardgamebot.game.entities import CardView, Side, Unit


def make_unit(uid: str, side: Side, **kw) -> Unit:
    defaults = dict(
        code=uid, name=uid, hp=40, max_hp=40, attack=5, defense=0, speed=10,
    )
    defaults.update(kw)
    return Unit(uid=uid, side=side, **defaults)


def catalog(*cards: CardView) -> dict[str, CardView]:
    return {c.code: c for c in cards}


STRIKE = CardView("strike", "타격", "", "attack", "enemy_single", 1, 1,
                  [{"op": "damage", "amount": 3}])
GUARD = CardView("guard", "방어", "", "defense", "self", 1, 1,
                 [{"op": "block", "amount": 8}])
FREE = CardView("free", "무료", "", "buff", "self", 0, 1, [])
EXPENSIVE = CardView("big", "대형", "", "attack", "enemy_single", 99, 1,
                     [{"op": "damage", "amount": 50}])


def build(player_deck: list[str], enemy_moves=None, **kw) -> CombatEngine:
    player = make_unit("p0", Side.PLAYER, draw_pile=list(player_deck))
    enemy = make_unit("e0", Side.ENEMY, hp=100, max_hp=100)
    return CombatEngine.start(
        units=[player, enemy],
        catalog=catalog(STRIKE, GUARD, FREE, EXPENSIVE),
        enemy_moves=enemy_moves or {"e0": [{"name": "때리기", "weight": 1,
                                            "target": "enemy_single",
                                            "effects": [{"op": "damage", "amount": 1}]}]},
        seed=42,
        **kw,
    )


# --- §2.2 드로우 & 핸드 ---------------------------------------------------


def test_draws_multiple_and_player_picks_one():
    engine = build(["strike"] * 10, draw_per_turn=3)
    assert engine.phase is BattlePhase.AWAITING_INPUT
    assert len(engine.pending.drawn) == 3


def test_unselected_cards_are_discarded_not_kept_as_hand():
    engine = build(["strike"] * 10, draw_per_turn=3)
    player = engine.unit("p0")
    engine.play(1)
    # 뽑은 3장이 전부 버림 더미로 간다 (사용한 1장 포함) — 핸드로 남지 않는다.
    assert len(player.discard_pile) == 3


def test_draw_pile_exhaustion_has_no_reshuffle():
    engine = build(["strike"], draw_per_turn=3)
    player = engine.unit("p0")
    assert len(engine.pending.drawn) == 1   # 1장뿐이라 1장만 뽑힌다
    engine.play(1)
    assert player.draw_pile == []

    # 다음 턴에는 버림 더미가 있어도 재섞기 없이 0장 드로우.
    engine.advance()
    while engine.phase is BattlePhase.RUNNING:
        engine.advance()
    assert engine.pending is not None
    assert engine.pending.drawn == []
    assert player.draw_pile == []


# --- §2.3 자원 -------------------------------------------------------------


def test_resource_is_shared_across_party_and_refills_each_round():
    p0 = make_unit("p0", Side.PLAYER, speed=20, draw_pile=["strike"] * 10)
    p1 = make_unit("p1", Side.PLAYER, speed=15, draw_pile=["strike"] * 10)
    enemy = make_unit("e0", Side.ENEMY, hp=200, max_hp=200, speed=1)
    engine = CombatEngine.start(
        units=[p0, p1, enemy],
        catalog=catalog(STRIKE, GUARD, FREE, EXPENSIVE),
        enemy_moves={"e0": []},
        resource_max=3,
        draw_per_turn=2,
        seed=1,
    )
    assert engine.resource == 3

    engine.play(1)                 # p0 가 1 소모
    # 다음 차례인 p1 이 줄어든 풀을 그대로 물려받는다 = 캐릭터별이 아니라 파티 공유
    assert engine.resource == 2
    assert engine.active_unit.uid == "p1"
    assert engine.round_number == 1

    engine.play(1)                 # 파티 전원이 행동을 마쳐 라운드가 끝난다
    # §2.3: 라운드가 넘어가는 시점에 자원이 완전 회복된다.
    assert engine.round_number == 2
    assert engine.resource == 3


def test_cannot_play_card_without_enough_resource():
    engine = build(["big"] * 5, draw_per_turn=2)
    with pytest.raises(CombatError, match="자원이 부족"):
        engine.play(1)


# --- §2.5 방어(블록) -------------------------------------------------------


def test_block_absorbs_damage_then_resets_at_own_turn_start():
    p0 = make_unit("p0", Side.PLAYER, speed=20, draw_pile=["guard"] * 10)
    enemy = make_unit("e0", Side.ENEMY, hp=100, max_hp=100, speed=1, attack=0)
    engine = CombatEngine.start(
        units=[p0, enemy],
        catalog=catalog(STRIKE, GUARD, FREE, EXPENSIVE),
        enemy_moves={"e0": [{"name": "일격", "weight": 1, "target": "enemy_single",
                             "effects": [{"op": "damage", "amount": 5}]}]},
        draw_per_turn=1,
        seed=7,
    )
    engine.play(1)                    # 블록 8 획득 → 적 공격 5 를 흡수
    assert p0.hp == p0.max_hp
    # 자기 턴이 다시 오면 블록은 사라진다 (영구 스탯 상승이 아님).
    assert p0.block == 0


# --- §2.1 턴 구조 ----------------------------------------------------------


def test_player_and_enemy_turns_interleave_individually():
    p0 = make_unit("p0", Side.PLAYER, speed=30, draw_pile=["free"] * 20)
    p1 = make_unit("p1", Side.PLAYER, speed=10, draw_pile=["free"] * 20)
    e0 = make_unit("e0", Side.ENEMY, speed=20, hp=100, max_hp=100)
    e1 = make_unit("e1", Side.ENEMY, speed=5, hp=100, max_hp=100)
    engine = CombatEngine.start(
        units=[p0, p1, e0, e1],
        catalog=catalog(FREE),
        enemy_moves={"e0": [], "e1": []},
        draw_per_turn=1,
        seed=3,
    )
    # 속도 순서: p0(30) → e0(20) → p1(10) → e1(5) — 진영별 일괄이 아니다.
    assert engine.active_unit.uid == "p0"
    engine.play(1)
    assert engine.active_unit.uid == "p1"   # 그 사이 e0 이 행동을 마쳤다


# --- 승패 -----------------------------------------------------------------


def test_battle_ends_when_all_enemies_die():
    p0 = make_unit("p0", Side.PLAYER, speed=20, attack=100, draw_pile=["strike"] * 5)
    e0 = make_unit("e0", Side.ENEMY, hp=5, max_hp=5, speed=1)
    engine = CombatEngine.start(
        units=[p0, e0], catalog=catalog(STRIKE), enemy_moves={"e0": []},
        draw_per_turn=1, seed=5,
    )
    engine.play(1)
    assert engine.phase is BattlePhase.WON


def test_battle_lost_when_party_wiped():
    p0 = make_unit("p0", Side.PLAYER, hp=1, max_hp=1, speed=1, draw_pile=["free"] * 5)
    e0 = make_unit("e0", Side.ENEMY, hp=100, max_hp=100, speed=20)
    engine = CombatEngine.start(
        units=[p0, e0], catalog=catalog(FREE),
        enemy_moves={"e0": [{"name": "일격", "weight": 1, "target": "enemy_single",
                             "effects": [{"op": "damage", "amount": 99}]}]},
        draw_per_turn=1, seed=11,
    )
    assert engine.phase is BattlePhase.LOST


# --- 직렬화 ---------------------------------------------------------------


def test_engine_round_trips_through_json():
    import json

    engine = build(["strike"] * 8, draw_per_turn=3)
    engine.play(1)
    snapshot = json.loads(json.dumps(engine.to_dict()))
    restored = CombatEngine.from_dict(snapshot)

    assert restored.phase is engine.phase
    assert restored.resource == engine.resource
    assert restored.round_number == engine.round_number
    assert [u.hp for u in restored.units] == [u.hp for u in engine.units]
    assert restored.pending.drawn == engine.pending.drawn
