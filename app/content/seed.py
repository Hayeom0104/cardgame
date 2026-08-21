"""Seed content — the minimum launch set named in §13.2.

Everything here is *content*, not logic: it is inserted through the same
validation layer the dashboard uses (§10.3), so nothing in this file can
express something the engine cannot execute.

P-2 (starter display name → 루야) and P-3 (run currency label → 실버) were
resolved in Design Doc v7 §13.1. Internal identifiers (`starter_001`,
`run_currency`) stay stable; only the display text changed.
"""

from __future__ import annotations

import json

from app.content.balance import seed_constants
from app.content.versioning import create_version, publish
from app.db.connection import Database, utcnow
from app.engine import passives as pv
from app.engine import statuses as st
from app.engine import timed_effects as te

STARTER_CHARACTER_ID = "starter_001"    # §4.6.1, display name 루야 (P-2, resolved v7)
TUTORIAL_WORLD_ID = "world_tutorial"
WORLD_1_ID = "world_1"

CARD_BASIC_ATTACK = "card_평타"
CARD_BASIC_DEFENSE = "card_기본방어"
CARD_STARTER_SKILL = "card_starter_화염참"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# =====================================================================
# §2.5.1 base status set (10)
# =====================================================================
def _seed_statuses(db: Database, version: int) -> None:
    magnitudes = {
        st.BURN: 3, st.BLEED: 4, st.DEFENSE_DOWN: 0.08, st.ATTACK_UP: 0.10,
        st.HEAL_DOWN: -0.15, st.SPEED_DOWN: -8, st.STUN: 0, st.SILENCE: 0,
        st.SHIELD_PIERCE: 0, st.TAUNT: 0,
    }
    rows = [
        # (id, name, kind, model, clock, stack_cap, base_duration)
        (st.BURN, "화상", "debuff", st.STACK_DECAY, st.TURN_START_TRIGGER, None, None),
        (st.BLEED, "출혈", "debuff", st.STACK_DECAY, st.TURN_START_TRIGGER, None, None),
        (st.STUN, "기절", "debuff", st.COUNTDOWN, st.OWNER_TURN_COUNTDOWN, None, 1),
        (st.SILENCE, "침묵", "debuff", st.COUNTDOWN, st.OWNER_TURN_COUNTDOWN, None, 2),
        (st.SHIELD_PIERCE, "보호막 관통", "debuff", st.COUNTDOWN,
         st.OWNER_TURN_COUNTDOWN, None, 2),
        # 도발 is a BUFF on an ALLY — a universal hostile-target override (§2.5.1).
        (st.TAUNT, "도발", "buff", st.COUNTDOWN, st.OWNER_TURN_COUNTDOWN, None, 2),
        (st.DEFENSE_DOWN, "방어력 감소", "debuff", st.STACK_DURATION,
         st.OWNER_TURN_COUNTDOWN, 5, 3),
        (st.ATTACK_UP, "공격력 증가", "buff", st.STACK_DURATION,
         st.OWNER_TURN_COUNTDOWN, 5, 3),
        (st.HEAL_DOWN, "회복량 감소", "debuff", st.STACK_DURATION,
         st.OWNER_TURN_COUNTDOWN, 4, 3),
        (st.SPEED_DOWN, "속도 감소", "debuff", st.STACK_DURATION,
         st.OWNER_TURN_COUNTDOWN, 4, 3),
    ]
    for status_id, name, kind, model, clock, cap, duration in rows:
        db.execute(
            "INSERT OR REPLACE INTO statuses (content_version_id, status_id, name, "
            "kind, model, clock, stack_cap, base_duration, magnitude, cleansable, "
            "persists_through_boss_phase, icon_asset, scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, ?)",
            (version, status_id, name, kind, model, clock, cap, duration,
             magnitudes[status_id], st.UNIVERSAL),
        )
    # §2.5.1a — 카드 업그레이드 전용 상태의 예시. `player_only`이므로 §10.5가
    # 적 액션과 저주받은 카드 풀에서 배제한다.
    db.execute(
        "INSERT OR REPLACE INTO statuses (content_version_id, status_id, name, "
        "kind, model, clock, stack_cap, base_duration, magnitude, cleansable, "
        "persists_through_boss_phase, icon_asset, scope) "
        "VALUES (?, '집중', '집중', 'buff', ?, ?, 3, 2, 0.05, 1, 0, NULL, ?)",
        (version, st.STACK_DURATION, st.OWNER_TURN_COUNTDOWN, st.PLAYER_ONLY),
    )


# =====================================================================
# §2.8.2 seed strategies (7) and §2.8.3 role defaults (3)
# =====================================================================
def _seed_strategies(db: Database, version: int) -> None:
    # v6.2's #7 (도발 우선) was removed and #8 renumbered to #7: 도발 is now a
    # universal override applied AFTER strategy selection (§2.5.1).
    rows = [
        ("strat_lowest_hp_abs", "최저 HP (절대값)", "lowest_hp_absolute", None, None, None),
        ("strat_lowest_hp_pct", "최저 HP%", "lowest_hp_percent", None, None, None),
        ("strat_random", "랜덤", "random_uniform", None, None, None),
        ("strat_highest_threat", "최고 위협도", "highest_score", "threat_score", None, None),
        ("strat_has_debuff", "디버프 보유 우선", "has_status", None,
         "strat_lowest_hp_pct", {"category": "debuff"}),
        ("strat_lacks_buff", "버프 없는 대상 우선", "lacks_status_category", None,
         None, {"category": "buff"}),
        ("strat_fixed_slot", "고정 타겟", "fixed_slot", None, None, {"visible_slot": 0}),
    ]
    for strategy_id, name, selector, scoring, fallback, params in rows:
        db.execute(
            "INSERT OR REPLACE INTO targeting_strategies (content_version_id, "
            "strategy_id, name, selector_operator, valid_target_filter, "
            "scoring_expression, tie_breaker, fallback_strategy_id, params_json) "
            "VALUES (?, ?, ?, ?, NULL, ?, 'registration_order', ?, ?)",
            (version, strategy_id, name, selector, scoring, fallback,
             _json(params) if params else None),
        )

    # The tanker previously defaulted to 도발 우선 with 최고 위협도 as fallback.
    # With 도발 a universal override, that pair collapsed into just 최고 위협도 —
    # and the taunt behaviour is unchanged.
    roles = [
        ("공격형", "공격형", "strat_lowest_hp_pct", None),
        ("방어형", "방어형/탱커형", "strat_highest_threat", None),
        ("서포터형", "서포터형", "strat_has_debuff", "strat_lowest_hp_pct"),
    ]
    for role_id, name, default, fallback in roles:
        db.execute(
            "INSERT OR REPLACE INTO enemy_roles (content_version_id, enemy_role_id, "
            "name, default_strategy_id, fallback_strategy_id) VALUES (?, ?, ?, ?, ?)",
            (version, role_id, name, default, fallback),
        )


def _seed_threat_weights(db: Database, version: int) -> None:
    """§15.3 — a new role needs a new row AND column; §10.5 rejects a partial grid."""
    grid = {
        "공격형":   {"공격형": 1.0, "방어형": 0.6, "서포터형": 2.5, "디버퍼형": 1.3, "딜서포트형": 1.3},
        "방어형":   {"공격형": 2.0, "방어형": 0.7, "서포터형": 1.0, "디버퍼형": 0.9, "딜서포트형": 1.4},
        "서포터형": {"공격형": 1.2, "방어형": 0.6, "서포터형": 1.1, "디버퍼형": 2.2, "딜서포트형": 1.0},
    }
    for enemy_role, row in grid.items():
        for ally_role, weight in row.items():
            db.execute(
                "INSERT OR REPLACE INTO threat_weights (content_version_id, "
                "enemy_role_id, ally_role_id, weight) VALUES (?, ?, ?, ?)",
                (version, enemy_role, ally_role, weight),
            )


# =====================================================================
# §4.3 / §4.6.2 starter cards and characters
# =====================================================================
def _seed_cards(db: Database, version: int) -> None:
    from app.content.balance import Balance

    card_cost_min = int(Balance(db, version).get("card_cost_min"))
    cards = [
        # 평타 and a basic defense card, both 무속성, guaranteeing playability.
        (CARD_BASIC_ATTACK, "평타", "무속성", 1, "공격", "enemy", 1,
         [{"operator": "deal_damage", "params": {"multiplier": 1.0}}]),
        (CARD_BASIC_DEFENSE, "기본 방어", "무속성", 1, "방어", "self", 1,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.5}}]),
        (CARD_STARTER_SKILL, "화염참", "화", 2, "공격", "enemy", 2,
         [{"operator": "deal_damage", "params": {"multiplier": 1.8}}]),
        ("card_화_강타", "화염 강타", "화", 3, "공격", "enemy", 3,
         [{"operator": "deal_damage", "params": {"multiplier": 2.6}},
          {"operator": "apply_status", "params": {"status_id": st.BURN, "stacks": 2}}]),
        ("card_수_치유", "물의 치유", "수", 2, "회복", "ally", 2,
         [{"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.25}}]),
        ("card_지_도발", "대지의 방벽", "지", 2, "버프디버프", "self", 3,
         [{"operator": "apply_status", "params": {"status_id": st.TAUNT}},
          {"operator": "grant_block", "params": {"mode": "multiplier", "value": 2.5}}]),
        ("card_암_약화", "그림자 약화", "암", 2, "버프디버프", "enemy", 4,
         [{"operator": "apply_status",
           "params": {"status_id": st.DEFENSE_DOWN, "stacks": 2}}]),
        ("card_광_각성", "빛의 각성", "광", 3, "버프디버프", "ally", 6,
         [{"operator": "apply_status",
           "params": {"status_id": st.ATTACK_UP, "stacks": 3}}]),
        # 여기부터는 §5.6의 여섯 등급이 고르게 차도록, 그리고 일곱 원소가
        # 전부 한 장 이상은 갖도록 채운 것들이다. 보상 칸과 상점이 등급별로
        # 뽑아 가므로 어느 등급이 비면 그 칸이 늘 같은 카드를 내놓는다.
        ("card_풍_질풍", "질풍베기", "풍", 1, "공격", "enemy", 2,
         [{"operator": "deal_damage", "params": {"multiplier": 1.3}}]),
        ("card_풍_가속", "순풍", "풍", 1, "버프디버프", "ally", 3,
         [{"operator": "modify_stat",
           "params": {"stat": "spd", "delta": 12, "duration_rounds": 2}}]),
        ("card_수_보호막", "물의 장막", "수", 2, "방어", "ally", 3,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 2.0}}]),
        ("card_지_흔들기", "대지 가르기", "지", 2, "공격", "all", 4,
         [{"operator": "deal_damage", "params": {"multiplier": 1.2}},
          {"operator": "apply_status",
           "params": {"status_id": st.SPEED_DOWN, "stacks": 1}}]),
        ("card_암_출혈", "그림자 칼날", "암", 2, "공격", "enemy", 3,
         [{"operator": "deal_damage", "params": {"multiplier": 1.5}},
          {"operator": "apply_status", "params": {"status_id": st.BLEED, "stacks": 3}}]),
        ("card_광_정화", "정화의 빛", "광", 2, "회복", "ally", 4,
         [{"operator": "remove_status", "params": {"category": "debuff", "count": 2}},
          {"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.15}}]),
        # 비용은 하드코딩하지 않고 card_cost_min 을 그대로 쓴다 — §10.5가
        # "카드 하나의 비용은 이 값 밑으로 내려갈 수 없다" 고 강제하므로, 이
        # 카드가 낼 수 있는 최저가여야 한다는 뜻 자체가 card_cost_min 이다.
        ("card_무_재정비", "재정비", "무속성", card_cost_min, "버프디버프", "self", 4,
         [{"operator": "draw_cards", "params": {"count": 1}},
          {"operator": "modify_resource", "params": {"delta": 1}}]),
        ("card_화_폭발", "연쇄 폭발", "화", 3, "공격", "all", 5,
         [{"operator": "deal_damage", "params": {"multiplier": 1.6}},
          {"operator": "apply_status", "params": {"status_id": st.BURN, "stacks": 2}}]),
        ("card_수_해일", "해일", "수", 3, "공격", "all", 5,
         [{"operator": "deal_damage", "params": {"multiplier": 1.7}},
          {"operator": "apply_status",
           "params": {"status_id": st.HEAL_DOWN, "stacks": 2}}]),
        ("card_암_심연", "심연의 손아귀", "암", 3, "공격", "enemy", 6,
         [{"operator": "deal_damage", "params": {"multiplier": 2.9}},
          {"operator": "apply_status", "params": {"status_id": st.STUN}}]),
        # 도발은 아군에게 거는 버프이므로 (§2.5.1) 대상은 시전자 자신이다.
        ("card_지_성벽", "불괴의 성벽", "지", 3, "방어", "self", 6,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 2.2}},
          {"operator": "apply_status", "params": {"status_id": st.TAUNT}}]),
    ]
    for card_id, name, element, cost, category, target_side, tier, effects in cards:
        db.execute(
            "INSERT OR REPLACE INTO cards (content_version_id, card_id, name, element, "
            "cost, category, target_side, rarity_tier, effects_json, art_asset, "
            "is_retired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (version, card_id, name, element, cost, category, target_side, tier,
             _json(effects)),
        )


def _seed_card_upgrades(db: Database, version: int) -> None:
    """§5.8 [v6.4] — 스타터 스킬의 5단계 업그레이드 경로.

    전이별 효과 선택은 §5.8.5대로 콘텐츠 작업이다. 여기 있는 것은 §5.8.3의
    규칙을 실제로 밟는 하나의 완전한 경로다:

        0→1  숫자 변경만
        1→2  숫자 변경 + `player_only` 상태, 약한 강도
        2→3  숫자 변경 + `player_only` 상태
        3→4  숫자 변경 + 모든 scope, 강한 강도
        4→5  숫자 변경 + 모든 scope
    """
    from app.content.balance import Balance

    balance = Balance(db, version)
    costs = balance.get("card_upgrade_costs")

    # 배율은 §15.1 밴드 안에 머문다: 표준 스킬(코스트 2)은 1.6–2.0.
    overlays = {
        1: [{"operator": "deal_damage", "params": {"multiplier": 1.9}}],
        2: [{"operator": "deal_damage", "params": {"multiplier": 2.0}},
            # 약한 강도의 player_only 상태 (§5.8.3).
            # 자기 버프이므로 카드의 target_side(enemy)가 아니라 시전자에게 (§5.8.3).
            {"operator": "apply_status",
             "params": {"status_id": "집중", "stacks": 1, "target": "self"}}],
        3: [{"operator": "deal_damage", "params": {"multiplier": 2.2}},
            {"operator": "apply_status",
             "params": {"status_id": "집중", "stacks": 2, "target": "self"}}],
        # 3→4부터 모든 scope가 열린다.
        4: [{"operator": "deal_damage", "params": {"multiplier": 2.5}},
            {"operator": "apply_status",
             "params": {"status_id": st.BURN, "stacks": 2}}],
        5: [{"operator": "deal_damage", "params": {"multiplier": 2.8}},
            {"operator": "apply_status",
             "params": {"status_id": st.BURN, "stacks": 3}},
            # 비용 감소도 숫자 변경의 한 형태다 (§5.8.3).
            {"operator": "modify_cost", "params": {"delta": -1}}],
    }

    for tier, overlay in overlays.items():
        cost = costs[str(tier)]
        db.execute(
            "INSERT OR REPLACE INTO card_upgrades (content_version_id, card_id, "
            "target_tier, fragment_cost, wildcard_cost, coin_cost, effects_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, CARD_STARTER_SKILL, tier, int(cost["fragments"]),
             int(cost["wildcards"]), int(cost["coin"]), _json(overlay)),
        )


def _seed_characters(db: Database, version: int) -> None:
    # §4.6.1 — the starter sits OUTSIDE the gacha pool, because a pullable
    # starter produces an ambiguous "duplicate" of a character never pulled.
    db.execute(
        "INSERT OR REPLACE INTO characters (content_version_id, character_id, name, "
        "element, job_role, base_rarity, special_cap, in_gacha_pool, art_asset, "
        "is_retired) VALUES (?, ?, ?, '화', '딜서포트형', 1, 0, 0, NULL, 0)",
        (version, STARTER_CHARACTER_ID, "루야"),   # P-2, resolved v7
    )
    # §4.5.2 seed examples — gacha-pool characters.
    for character_id, name, element, role, rarity in [
        ("char_ignis", "이그니스", "화", "공격형", 2),
        ("char_aquel", "아쿠엘", "수", "서포터형", 3),
        ("char_terradon", "테라돈", "지", "방어형", 1),
        ("char_umbra", "움브라", "암", "디버퍼형", 1),
        # §15.4의 등급별 성급(top 3 / mid 2 / base 1)에 셋 다 후보가 있어야
        # 그 등급이 나왔을 때 같은 캐릭터만 반복해서 뽑히지 않는다.
        ("char_ventus", "벤투스", "풍", "딜서포트형", 2),
        ("char_lumen", "루멘", "광", "서포터형", 3),
        ("char_silva", "실바", "지", "공격형", 1),
    ]:
        db.execute(
            "INSERT OR REPLACE INTO characters (content_version_id, character_id, "
            "name, element, job_role, base_rarity, special_cap, in_gacha_pool, "
            "art_asset, is_retired) VALUES (?, ?, ?, ?, ?, ?, 0, 1, NULL, 0)",
            (version, character_id, name, element, role, rarity),
        )


# =====================================================================
# §2.7.2 seed cursed cards (4) — magnitudes from §15.5
# =====================================================================
def _seed_cursed_cards(db: Database, version: int) -> None:
    rows = [
        ("curse_고통의각인", "고통의 각인",
         [{"operator": "deal_flat_damage",
           "params": {"amount": 8, "ignores_block": True, "ignores_defense": True}}],
         "각인이 살을 파고든다."),
        ("curse_무거운사슬", "무거운 사슬",
         [{"operator": "modify_resource", "params": {"delta": -2}}],
         "사슬이 팀 전체를 짓누른다."),
        ("curse_부식된갑주", "부식된 갑주",
         [{"operator": "apply_status",
           "params": {"status_id": st.DEFENSE_DOWN, "stacks": 2}}],
         "갑주가 부스러진다."),
        ("curse_흐려진시야", "흐려진 시야",
         [{"operator": "apply_status",
           "params": {"status_id": st.SPEED_DOWN, "stacks": 2}}],
         "시야가 흐려 발이 무겁다."),
    ]
    for cursed_id, name, penalty, flavor in rows:
        db.execute(
            "INSERT OR REPLACE INTO cursed_cards (content_version_id, cursed_card_id, "
            "name, art_asset, penalty_json, flavor_text, removable) "
            "VALUES (?, ?, ?, NULL, ?, ?, 1)",
            (version, cursed_id, name, _json(penalty), flavor),
        )


# =====================================================================
# §2.8.4 transition effects (2) — both TIMED EFFECTS, not statuses (§15.6)
# =====================================================================
def _seed_transition_effects(db: Database, version: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO transition_effects (content_version_id, "
        "transition_effect_id, name, effects_json) VALUES (?, 'trans_무적', '무적', ?)",
        (version, _json([{"operator": "set_invulnerable",
                          "params": {"duration_rounds": 1}}])),
    )
    # 각성 inserts two stat_modifier rows. It does NOT apply the 공격력 증가
    # status: that status's clock is OWNER_TURN_COUNTDOWN and one instance
    # cannot obey both its own clock and a boss-specific round clock (B-03).
    db.execute(
        "INSERT OR REPLACE INTO transition_effects (content_version_id, "
        "transition_effect_id, name, effects_json) VALUES (?, 'trans_각성', '각성', ?)",
        (version, _json([
            {"operator": "modify_stat",
             "params": {"stat": "atk", "delta": 30, "is_percent": True,
                        "duration_rounds": 3}},
            {"operator": "modify_stat",
             "params": {"stat": "spd", "delta": 15, "is_percent": False,
                        "duration_rounds": 3}},
        ])),
    )


# =====================================================================
# Enemies, actions, encounters, worlds
# =====================================================================
def _seed_enemy_actions(db: Database, version: int) -> None:
    rows = [
        ("act_기본공격", "기본 공격", "공격", "enemy", 1,
         [{"operator": "deal_damage", "params": {"multiplier": 1.0}}]),
        ("act_강타", "강타", "공격", "enemy", 0,
         [{"operator": "deal_damage", "params": {"multiplier": 1.6}}]),
        ("act_화상부여", "불꽃 낙인", "버프디버프", "enemy", 0,
         [{"operator": "apply_status", "params": {"status_id": st.BURN, "stacks": 2}}]),
        ("act_boss_광역", "심연의 파도", "공격", "all", 0,
         [{"operator": "deal_damage", "params": {"multiplier": 1.2}}]),
        ("act_boss_기절", "각인의 일격", "공격", "enemy", 0,
         [{"operator": "deal_damage", "params": {"multiplier": 1.4}},
          {"operator": "apply_status", "params": {"status_id": st.STUN}}]),
        # 2세계 이후의 적이 쓰는 것들. 새 상태를 만들지 않고 §2.5.1a의 기존
        # 10개만 쓴다 — 상태를 늘리면 §10.5의 scope 판정이 함께 늘어난다.
        ("act_방벽", "방벽", "방어", "self", 0,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 2.0}}]),
        ("act_출혈", "찢는 발톱", "공격", "enemy", 0,
         [{"operator": "deal_damage", "params": {"multiplier": 1.1}},
          {"operator": "apply_status", "params": {"status_id": st.BLEED, "stacks": 3}}]),
        ("act_광역약화", "무력화의 안개", "버프디버프", "all", 0,
         [{"operator": "apply_status",
           "params": {"status_id": st.DEFENSE_DOWN, "stacks": 2}}]),
        ("act_침묵", "봉인의 주문", "버프디버프", "enemy", 0,
         [{"operator": "apply_status", "params": {"status_id": st.SILENCE}}]),
        ("act_감속", "진흙 발목", "버프디버프", "enemy", 0,
         [{"operator": "apply_status",
           "params": {"status_id": st.SPEED_DOWN, "stacks": 2}}]),
        ("act_회복저해", "썩은 숨결", "버프디버프", "all", 0,
         [{"operator": "apply_status",
           "params": {"status_id": st.HEAL_DOWN, "stacks": 2}}]),
        ("act_boss_돌진", "짓밟기", "공격", "enemy", 0,
         [{"operator": "deal_damage", "params": {"multiplier": 1.8}}]),
        ("act_boss_포효", "포효", "버프디버프", "self", 0,
         [{"operator": "apply_status",
           "params": {"status_id": st.ATTACK_UP, "stacks": 2}},
          {"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.5}}]),
        # 특수(변칙) 적용 행동 — 오너 승인으로 세계마다 한둘씩 추가한
        # 콘텐츠다(설계 문서에 근거를 둔 수치가 아니라 기존 패턴을 따라
        # 새로 지은 것). §2.5.1a의 기존 10개 상태와 기존 연산자만 쓴다 —
        # 상태를 늘리면 §10.5의 scope 판정이 함께 늘어난다.
        ("act_아군투지", "투지 고취", "버프디버프", "ally", 0,
         [{"operator": "apply_status", "params": {"status_id": st.ATTACK_UP, "stacks": 2}}]),
        ("act_필사반격", "필사의 각오", "버프디버프", "self", 0,
         [{"operator": "modify_stat",
           "params": {"stat": "atk", "delta": 30, "is_percent": True,
                      "duration_rounds": 2}}]),
        ("act_아군치유", "치유의 손길", "회복", "ally", 0,
         [{"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.25}}]),
        ("act_무적화", "굳건한 수호", "방어", "self", 0,
         [{"operator": "set_invulnerable", "params": {"duration_rounds": 1}}]),
        ("act_소환_하피", "폭풍 부르기", "버프디버프", "self", 0,
         [{"operator": "summon_enemy",
           "params": {"enemy_id": "enemy_w3_하피", "count": 1}}]),
        ("act_가속", "질풍 가속", "버프디버프", "self", 0,
         [{"operator": "modify_stat",
           "params": {"stat": "spd", "delta": 15, "is_percent": False,
                      "duration_rounds": 3}}]),
        ("act_자가치유", "부패의 재생", "회복", "self", 0,
         [{"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.20}}]),
    ]
    for action_id, name, category, target_side, is_basic, effects in rows:
        db.execute(
            "INSERT OR REPLACE INTO enemy_actions (content_version_id, action_id, "
            "name, category, target_side, is_basic_attack, effects_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, action_id, name, category, target_side, is_basic, _json(effects)),
        )


def _seed_enemies(db: Database, version: int) -> None:
    common_rules = [
        {"priority": 10, "condition": None, "action_id": "act_기본공격", "weight": 3.0,
         "cooldown_turns": 0},
        {"priority": 20, "condition": None, "action_id": "act_강타", "weight": 1.0,
         "cooldown_turns": 1},
    ]
    rows = [
        # tutorial band — §15.9: HP 30-42 / 공격 9-13 / 방어 2-4 / 속도 80-100
        ("enemy_tut_슬라임", "훈련용 슬라임", "일반", "무속성", "공격형", 36, 10, 3, 90,
         common_rules),
        ("enemy_tut_박쥐", "동굴 박쥐", "일반", "암", "공격형", 30, 11, 2, 100,
         common_rules),
        # main campaign 일반 band — HP 45-70 / 공격 8-12 / 방어 3-6 / 속도 80-110
        ("enemy_w1_고블린", "고블린", "일반", "지", "공격형", 55, 11, 4, 95, common_rules),
        ("enemy_w1_방패병", "고블린 방패병", "일반", "지", "방어형", 70, 8, 6, 82,
         common_rules),
        ("enemy_w1_주술사", "고블린 주술사", "일반", "화", "서포터형", 48, 9, 3, 105,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_화상부여", "weight": 1.0, "cooldown_turns": 2},
         ]),
        ("enemy_w1_늑대조련사", "고블린 늑대조련사", "일반", "지", "서포터형", 50, 9, 4, 98,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_아군투지", "weight": 1.0, "cooldown_turns": 3},
         ]),
        ("enemy_w1_돌격병", "고블린 돌격병", "일반", "지", "공격형", 58, 13, 3, 105,
         common_rules + [
             {"priority": 5, "condition": {"op": "self_hp_below", "value": 0.5},
              "action_id": "act_필사반격", "weight": 2.0, "cooldown_turns": 3},
         ]),
        # 2세계 — 늪. 굳히고 늦추는 쪽으로 몰아간다.
        ("enemy_w2_도롱뇽", "늪지 도롱뇽", "일반", "수", "공격형", 58, 10, 5, 88,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_감속", "weight": 1.0, "cooldown_turns": 2},
         ]),
        ("enemy_w2_석상", "이끼 낀 석상", "일반", "지", "방어형", 70, 8, 6, 80,
         common_rules + [
             {"priority": 5, "condition": {"op": "self_hp_below", "value": 0.6},
              "action_id": "act_방벽", "weight": 2.0, "cooldown_turns": 2},
         ]),
        ("enemy_w2_망령", "늪의 망령", "일반", "암", "서포터형", 46, 9, 3, 108,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_회복저해", "weight": 1.0, "cooldown_turns": 3},
         ]),
        ("enemy_w2_거머리", "거대 거머리", "엘리트", "수", "공격형", 130, 16, 7, 100,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_출혈", "weight": 2.0, "cooldown_turns": 2},
         ]),
        ("enemy_w2_늪치유사", "늪지 치유사", "일반", "수", "서포터형", 48, 8, 4, 90,
         common_rules + [
             {"priority": 5, "condition": {"op": "any_ally_hp_below", "value": 0.6},
              "action_id": "act_아군치유", "weight": 2.0, "cooldown_turns": 3},
         ]),
        ("enemy_w2_가시덩굴", "가시 넝쿨", "일반", "목", "방어형", 66, 9, 7, 78,
         common_rules + [
             {"priority": 5, "condition": {"op": "own_side_count_below", "value": 2},
              "action_id": "act_무적화", "weight": 2.0, "cooldown_turns": 4},
         ]),
        # 3세계 — 절벽. 빠르고 성가시다.
        ("enemy_w3_하피", "절벽 하피", "일반", "풍", "공격형", 50, 12, 3, 110,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_출혈", "weight": 1.0, "cooldown_turns": 2},
         ]),
        ("enemy_w3_주문사", "바람 주문사", "일반", "풍", "서포터형", 47, 9, 4, 106,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_침묵", "weight": 1.0, "cooldown_turns": 3},
         ]),
        ("enemy_w3_수문장", "산정의 수문장", "엘리트", "지", "방어형", 148, 15, 10, 92,
         common_rules + [
             {"priority": 5, "condition": {"op": "self_hp_below", "value": 0.5},
              "action_id": "act_방벽", "weight": 2.0, "cooldown_turns": 1},
         ]),
        ("enemy_w3_폭풍소환사", "폭풍 소환사", "엘리트", "풍", "서포터형", 100, 10, 6, 95,
         common_rules + [
             {"priority": 5, "condition": {"op": "round_number_gte", "value": 3},
              "action_id": "act_소환_하피", "weight": 2.0, "cooldown_turns": 6},
         ]),
        ("enemy_w3_돌풍매", "돌풍매", "일반", "풍", "공격형", 52, 11, 3, 100,
         common_rules + [
             {"priority": 5, "condition": {"op": "round_number_gte", "value": 2},
              "action_id": "act_가속", "weight": 1.5, "cooldown_turns": 3},
         ]),
        # 4세계 — 심연. 광역과 디버프가 겹친다.
        ("enemy_w4_그림자", "그림자 병사", "일반", "암", "공격형", 62, 12, 5, 98,
         common_rules),
        ("enemy_w4_봉인관", "봉인의 관", "일반", "암", "서포터형", 52, 8, 5, 84,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_광역약화", "weight": 1.0, "cooldown_turns": 3},
         ]),
        ("enemy_w4_집행자", "심연의 집행자", "엘리트", "암", "공격형", 145, 18, 8, 112,
         common_rules + [
             {"priority": 5, "condition": {"op": "always"},
              "action_id": "act_boss_돌진", "weight": 1.5, "cooldown_turns": 2},
         ]),
        ("enemy_w4_재생하는망자", "재생하는 망자", "일반", "암", "방어형", 60, 10, 6, 85,
         common_rules + [
             {"priority": 5, "condition": {"op": "self_hp_below", "value": 0.5},
              "action_id": "act_자가치유", "weight": 2.0, "cooldown_turns": 3},
         ]),
        ("enemy_w4_무적파수꾼", "무적의 파수꾼", "엘리트", "암", "방어형", 140, 14, 9, 90,
         common_rules + [
             {"priority": 5, "condition": {"op": "owner_turn_index_mod",
                                          "divisor": 3, "value": 0},
              "action_id": "act_무적화", "weight": 2.0, "cooldown_turns": 4},
         ]),
    ]
    for enemy_id, name, tier, element, role, hp, atk, defense, spd, rules in rows:
        db.execute(
            "INSERT OR REPLACE INTO enemies (content_version_id, enemy_id, name, tier, "
            "element, role, hp, atk, def, spd, strategy_override, action_rules_json, "
            "art_asset, is_retired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, "
            "NULL, 0)",
            (version, enemy_id, name, tier, element, role, hp, atk, defense, spd,
             _json(rules)),
        )

    # §15.9 tutorial boss: HP 100, 공격 10-13, 방어 4, 속도 95, 2 phases.
    boss_rules = [
        {"priority": 10, "condition": None, "action_id": "act_기본공격", "weight": 3.0,
         "cooldown_turns": 0},
        {"priority": 5, "condition": {"op": "self_hp_below", "value": 0.5},
         "action_id": "act_boss_기절", "weight": 1.0, "cooldown_turns": 2,
         "min_phase": 2},
    ]
    db.execute(
        "INSERT OR REPLACE INTO enemies (content_version_id, enemy_id, name, tier, "
        "element, role, hp, atk, def, spd, strategy_override, action_rules_json, "
        "art_asset, is_retired) VALUES (?, 'enemy_tut_boss', '각인된 수호자', '보스', "
        "'암', '방어형', 100, 11, 4, 95, NULL, ?, NULL, 0)",
        (version, _json(boss_rules)),
    )
    # Tutorial boss transition: 무적 1 round on entering phase 2 (50%).
    db.execute(
        "INSERT OR REPLACE INTO boss_phases (content_version_id, boss_phase_id, "
        "enemy_id, phase_index, hp_threshold_pct, effect_ids_json) "
        "VALUES (?, 'phase_tut_boss_2', 'enemy_tut_boss', 2, 0.50, ?)",
        (version, _json(["trans_무적"])),
    )

    # 본편 보스 넷. 능력치는 §15의 보스 밴드 안이고, 2페이즈에서 각자 다른
    # 무기를 꺼낸다 — 같은 보스를 네 번 만나는 것처럼 느껴지지 않도록.
    campaign_bosses = [
        ("enemy_w1_boss", "고블린 대족장", "지", "공격형", "act_boss_돌진"),
        ("enemy_w2_boss", "늪의 군주", "수", "방어형", "act_회복저해"),
        ("enemy_w3_boss", "폭풍의 종자", "풍", "공격형", "act_침묵"),
        ("enemy_w4_boss", "심연의 군주", "암", "공격형", "act_boss_광역"),
    ]
    for enemy_id, name, element, role, signature in campaign_bosses:
        rules = [
            {"priority": 10, "condition": None, "action_id": "act_기본공격",
             "weight": 3.0, "cooldown_turns": 0},
            {"priority": 8, "condition": None, "action_id": "act_강타",
             "weight": 1.5, "cooldown_turns": 1},
            # 2페이즈에 들어가야 열리는 수. 그전에는 고르지 않는다.
            {"priority": 5, "condition": {"op": "always"},
             "action_id": signature, "weight": 2.0, "cooldown_turns": 2,
             "min_phase": 2},
            {"priority": 6, "condition": {"op": "self_hp_below", "value": 0.35},
             "action_id": "act_boss_포효", "weight": 1.0, "cooldown_turns": 3},
        ]
        db.execute(
            "INSERT OR REPLACE INTO enemies (content_version_id, enemy_id, name, "
            "tier, element, role, hp, atk, def, spd, strategy_override, "
            "action_rules_json, art_asset, is_retired) "
            "VALUES (?, ?, ?, '보스', ?, ?, 450, 22, 10, 100, NULL, ?, NULL, 0)",
            (version, enemy_id, name, element, role, _json(rules)),
        )
        db.execute(
            "INSERT OR REPLACE INTO boss_phases (content_version_id, boss_phase_id, "
            "enemy_id, phase_index, hp_threshold_pct, effect_ids_json) "
            "VALUES (?, ?, ?, 2, 0.50, ?)",
            (version, f"phase_{enemy_id}_2", enemy_id, _json(["trans_무적"])),
        )


def _seed_encounters(db: Database, version: int) -> None:
    rows = [
        ("enc_tut_1", TUTORIAL_WORLD_ID, "normal",
         [{"enemy_id": "enemy_tut_슬라임", "slot": 0}]),
        ("enc_tut_2", TUTORIAL_WORLD_ID, "normal",
         [{"enemy_id": "enemy_tut_슬라임", "slot": 0},
          {"enemy_id": "enemy_tut_박쥐", "slot": 1}]),
        ("enc_tut_boss", TUTORIAL_WORLD_ID, "boss",
         [{"enemy_id": "enemy_tut_boss", "slot": 0}]),
        ("enc_w1_normal", WORLD_1_ID, "normal",
         [{"enemy_id": "enemy_w1_고블린", "slot": 0},
          {"enemy_id": "enemy_w1_방패병", "slot": 1},
          {"enemy_id": "enemy_w1_주술사", "slot": 2}]),
        # 월드마다 전투 조우를 둘 이상 두는 것은 다양성 때문만이 아니다 —
        # 하나뿐이면 일곱 개의 전투 칸이 전부 같은 싸움이 된다.
        ("enc_w1_normal_2", WORLD_1_ID, "normal",
         [{"enemy_id": "enemy_w1_고블린", "slot": 0},
          {"enemy_id": "enemy_w1_고블린", "slot": 1}]),
        ("enc_w1_boss", WORLD_1_ID, "boss",
         [{"enemy_id": "enemy_w1_boss", "slot": 0}]),
        ("enc_w1_normal_3", WORLD_1_ID, "normal",
         [{"enemy_id": "enemy_w1_늑대조련사", "slot": 0},
          {"enemy_id": "enemy_w1_돌격병", "slot": 1},
          {"enemy_id": "enemy_w1_고블린", "slot": 2}]),

        ("enc_w2_normal", "world_2", "normal",
         [{"enemy_id": "enemy_w2_도롱뇽", "slot": 0},
          {"enemy_id": "enemy_w2_석상", "slot": 1}]),
        ("enc_w2_normal_2", "world_2", "normal",
         [{"enemy_id": "enemy_w2_도롱뇽", "slot": 0},
          {"enemy_id": "enemy_w2_망령", "slot": 1},
          {"enemy_id": "enemy_w2_망령", "slot": 2}]),
        ("enc_w2_normal_3", "world_2", "normal",
         [{"enemy_id": "enemy_w2_늪치유사", "slot": 0},
          {"enemy_id": "enemy_w2_가시덩굴", "slot": 1}]),
        ("enc_w2_elite", "world_2", "elite",
         [{"enemy_id": "enemy_w2_거머리", "slot": 0}]),
        ("enc_w2_boss", "world_2", "boss",
         [{"enemy_id": "enemy_w2_boss", "slot": 0}]),

        ("enc_w3_normal", "world_3", "normal",
         [{"enemy_id": "enemy_w3_하피", "slot": 0},
          {"enemy_id": "enemy_w3_하피", "slot": 1},
          {"enemy_id": "enemy_w3_주문사", "slot": 2}]),
        ("enc_w3_normal_2", "world_3", "normal",
         [{"enemy_id": "enemy_w3_주문사", "slot": 0},
          {"enemy_id": "enemy_w2_석상", "slot": 1}]),
        ("enc_w3_normal_3", "world_3", "normal",
         [{"enemy_id": "enemy_w3_돌풍매", "slot": 0},
          {"enemy_id": "enemy_w3_돌풍매", "slot": 1}]),
        ("enc_w3_elite", "world_3", "elite",
         [{"enemy_id": "enemy_w3_수문장", "slot": 0},
          {"enemy_id": "enemy_w3_하피", "slot": 1}]),
        ("enc_w3_elite_2", "world_3", "elite",
         [{"enemy_id": "enemy_w3_폭풍소환사", "slot": 0},
          {"enemy_id": "enemy_w3_돌풍매", "slot": 1}]),
        ("enc_w3_boss", "world_3", "boss",
         [{"enemy_id": "enemy_w3_boss", "slot": 0}]),

        ("enc_w4_normal", "world_4", "normal",
         [{"enemy_id": "enemy_w4_그림자", "slot": 0},
          {"enemy_id": "enemy_w4_그림자", "slot": 1},
          {"enemy_id": "enemy_w4_봉인관", "slot": 2}]),
        ("enc_w4_normal_2", "world_4", "normal",
         [{"enemy_id": "enemy_w4_그림자", "slot": 0},
          {"enemy_id": "enemy_w3_주문사", "slot": 1},
          {"enemy_id": "enemy_w4_봉인관", "slot": 2}]),
        ("enc_w4_normal_3", "world_4", "normal",
         [{"enemy_id": "enemy_w4_재생하는망자", "slot": 0},
          {"enemy_id": "enemy_w4_그림자", "slot": 1}]),
        ("enc_w4_elite", "world_4", "elite",
         [{"enemy_id": "enemy_w4_집행자", "slot": 0},
          {"enemy_id": "enemy_w4_봉인관", "slot": 1}]),
        ("enc_w4_elite_2", "world_4", "elite",
         [{"enemy_id": "enemy_w4_무적파수꾼", "slot": 0}]),
        ("enc_w4_boss", "world_4", "boss",
         [{"enemy_id": "enemy_w4_boss", "slot": 0}]),
    ]
    for encounter_id, world_id, kind, units in rows:
        db.execute(
            "INSERT OR REPLACE INTO encounters (content_version_id, encounter_id, "
            "world_id, kind, units_json) VALUES (?, ?, ?, ?, ?)",
            (version, encounter_id, world_id, kind, _json(units)),
        )


def _seed_worlds(db: Database, version: int) -> None:
    # §8.6.1 — world 1 is uniformly T0 BECAUSE its band has one value.
    rows = [
        (TUTORIAL_WORLD_ID, "튜토리얼", 0, 1, 0, 0),
        (WORLD_1_ID, "1세계 - 고블린 굴", 1, 0, 0, 0),
        ("world_2", "2세계 - 잠긴 늪", 2, 0, 0, 1),
        ("world_3", "3세계 - 바람의 절벽", 3, 0, 1, 2),
        ("world_4", "4세계 - 심연의 문", 4, 0, 2, 3),
    ]
    for world_id, name, sequence, is_tutorial, tier_min, tier_max in rows:
        db.execute(
            "INSERT OR REPLACE INTO worlds (content_version_id, world_id, name, "
            "sequence_index, is_tutorial, drop_tier_min, drop_tier_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, world_id, name, sequence, is_tutorial, tier_min, tier_max),
        )


# =====================================================================
# §3.3.1 seed events (8)
# =====================================================================
def _seed_events(db: Database, version: int) -> None:
    rows = [
        ("event_낡은상자", "낡은 상자", "choice", "none", [
            {"label": "안전하게 열기",
             "effects": [{"operator": "grant_currency",
                          "params": {"currency": "run_currency", "amount": 40}}]},
            {"label": "억지로 비틀기",
             "effects": [{"operator": "grant_card_fragments",
                          "params": {"card_id": CARD_STARTER_SKILL, "amount": 120}}]},
        ]),
        ("event_떠도는상인", "떠도는 상인", "instant", "none", [
            {"label": "선물 받기",
             "effects": [{"operator": "grant_currency",
                          "params": {"currency": "run_currency", "amount": 55}}]},
        ]),
        ("event_봉인된제단", "봉인된 제단", "choice", "none", [
            {"label": "지나친다", "effects": []},
            {"label": "정화한다",
             "effects": [{"operator": "remove_cursed_card", "params": {"count": 1}}]},
        ]),
        # Events 4 and 8 use TERMINAL_STATE_TRANSITION and therefore transition
        # the lifecycle rather than returning to the map (§16.2).
        ("event_매복", "매복", "instant", "fixed", [
            {"label": "싸운다",
             "effects": [{"operator": "start_combat",
                          "params": {"encounter_id": "enc_w1_normal"}}]},
        ]),
        ("event_수상한행상", "수상한 행상", "choice", "none", [
            {"label": "카드를 산다",
             "effects": [{"operator": "add_card_to_run_deck",
                          "params": {"card_id": "card_화_강타",
                                     "recipient": "chooser"}}]},
            {"label": "지나친다", "effects": []},
        ]),
        ("event_낙석", "낙석", "instant", "none", [
            {"label": "피할 수 없다",
             "effects": [{"operator": "modify_hp",
                          "params": {"mode": "percent_max_hp", "delta": -0.12}}]},
        ]),
        ("event_유령의속삭임", "유령의 속삭임", "choice", "none", [
            {"label": "무시한다", "effects": []},
            {"label": "귀를 기울인다",
             "effects": [{"operator": "grant_currency",
                          "params": {"currency": "carta", "amount": 200}}]},
        ]),
        ("event_버려진야영지", "버려진 야영지", "choice", "conditional", [
            {"label": "휴식",
             "effects": [{"operator": "heal",
                          "params": {"mode": "percent_max_hp", "value": 0.30}}]},
            {"label": "수색",
             "effects": [{"operator": "grant_equipment",
                          "params": {"rarity_band": "low"}}]},
        ]),
        # 여기부터 넷은 "고른 만큼 잃는" 쪽이다. 앞의 여덟 개가 대체로
        # 이득이라 이벤트 칸이 그냥 공짜 보상 칸처럼 굳어 있었다.
        ("event_피의계약", "피의 계약", "choice", "none", [
            {"label": "거절한다", "effects": []},
            {"label": "손을 벤다",
             "effects": [{"operator": "modify_hp",
                          "params": {"mode": "percent_max_hp", "delta": -0.20}},
                         {"operator": "grant_equipment",
                          "params": {"rarity_band": "high"}}]},
        ]),
        ("event_뒤틀린제단", "뒤틀린 제단", "choice", "none", [
            {"label": "물러선다", "effects": []},
            {"label": "제물을 바친다",
             "effects": [{"operator": "insert_cursed_card",
                          "params": {"cursed_card_id": "curse_무거운사슬"}},
                         {"operator": "grant_currency",
                          "params": {"currency": "run_currency", "amount": 120}}]},
        ]),
        ("event_대장간", "잊힌 대장간", "choice", "none", [
            {"label": "장비를 손본다",
             "effects": [{"operator": "grant_enhancement_stone",
                          "params": {"tier": 1, "amount": 3}}]},
            {"label": "재료만 챙긴다",
             "effects": [{"operator": "grant_currency",
                          "params": {"currency": "run_currency", "amount": 60}}]},
        ]),
        # `offer_reward` 를 실제로 쓰는 이벤트. 이 연산자는 등록도 검증도
        # 되어 있으면서 어느 콘텐츠도 쓰지 않아, 그것이 만든 보류 선택을
        # 처리하는 코드가 없다는 사실이 드러나지 않았다.
        ("event_원소무기고", "원소 무기고", "choice", "none", [
            {"label": "무기를 하나 고른다",
             "effects": [{"operator": "offer_reward",
                          "params": {"reward_table_id": "reward_원소무기고"}}]},
            {"label": "손대지 않는다", "effects": []},
        ]),
        ("event_길잃은학자", "길 잃은 학자", "choice", "none", [
            {"label": "길을 알려준다",
             "effects": [{"operator": "grant_currency",
                          "params": {"currency": "carta", "amount": 120}}]},
            {"label": "책을 빌린다",
             "effects": [{"operator": "grant_card_fragments",
                          "params": {"card_id": "card_광_정화", "amount": 80}}]},
        ]),
    ]
    for event_id, name, kind, combat_link, branches in rows:
        db.execute(
            "INSERT OR REPLACE INTO events (content_version_id, event_id, name, "
            "interaction_kind, branches_json, combat_link) VALUES (?, ?, ?, ?, ?, ?)",
            (version, event_id, name, kind, _json(branches), combat_link),
        )


# =====================================================================
# §20.4 seed achievements and §9.2 research nodes
# =====================================================================
def _seed_reward_tables(db: Database, version: int) -> None:
    """§10.4 `offer_reward` 가 뽑아 갈 목록.

    보상 칸(§3.2)은 계정이 해금한 카드에서 고르지만, 이벤트가 주는 보상은
    "이 이벤트에서만 나오는 것" 이어야 의미가 있다. 그 목록이 여기다.

    `weight` 는 서로에 대한 비율일 뿐이라 합이 1일 필요가 없다.
    """
    tables = {
        "reward_원소무기고": [
            {"card_id": "card_화_강타", "weight": 1.0},
            {"card_id": "card_수_보호막", "weight": 1.0},
            {"card_id": "card_풍_질풍", "weight": 1.0},
            {"card_id": "card_암_출혈", "weight": 1.0},
            {"card_id": "card_광_정화", "weight": 0.5},
        ],
    }
    for table_id, entries in tables.items():
        db.execute(
            "INSERT OR REPLACE INTO reward_tables (content_version_id, "
            "reward_table_id, entries_json) VALUES (?, ?, ?)",
            (version, table_id, _json(entries)),
        )


def _seed_passives(db: Database, version: int) -> None:
    """§6 패시브 카드.

    §6이 확정한 것은 구조다 — 파티 공유 슬롯, 런 단위 장착, 가챠 해금,
    6등급 체계, 그리고 "상시" 와 "전투 중 조건부" 두 발동 방식. 효과 목록
    자체는 문서에 없다. 그래서 여기 있는 8장은 **그 구조를 빠짐없이 밟는
    출시용 세트**이지 확정된 기획이 아니다: 등급 1~6이 모두 한 번씩 나오고,
    두 발동 방식이 모두 쓰이고, 파티 대상과 적 대상이 모두 있다.

    수치를 바꾸거나 장수를 늘리는 것은 관리자 대시보드에서 초안을 열어
    하면 되고, 코드는 손대지 않아도 된다.
    """
    rows = [
        # (id, 이름, 설명, 등급, 발동, 조건, 대상, 1전투 제한, 효과)
        ("pas_예리함", "예리함", "전투 내내 파티의 공격력이 오릅니다.", 1,
         pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0,
         [{"operator": "modify_stat",
           "params": {"stat": "atk", "delta": 2, "duration_rounds": te.BATTLE_LONG}}]),
        ("pas_굳은가죽", "굳은 가죽", "전투 내내 파티의 방어력이 오릅니다.", 2,
         pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0,
         [{"operator": "modify_stat",
           "params": {"stat": "def", "delta": 2, "duration_rounds": te.BATTLE_LONG}}]),
        ("pas_선제방벽", "선제 방벽", "전투를 시작할 때 파티가 방어도를 얻습니다.", 2,
         pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.0}}]),
        ("pas_경보", "경보", "전투 내내 파티의 속도가 오릅니다.", 3,
         pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0,
         [{"operator": "modify_stat",
           "params": {"stat": "spd", "delta": 5, "duration_rounds": te.BATTLE_LONG}}]),
        ("pas_전열정비", "전열 정비", "라운드가 시작할 때마다 파티가 방어도를 얻습니다.", 4,
         pv.TRIGGER_ROUND_START, {}, pv.SCOPE_PARTY, 0,
         [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 0.5}}]),
        # 조건부 발동 (§6) — 한 전투에 한 번만.
        ("pas_역전의호흡", "역전의 호흡",
         "파티원이 절반 아래로 떨어진 라운드에 자원을 더 얻습니다. 전투당 1회.", 4,
         pv.TRIGGER_ROUND_START, {"hp_below": 0.5}, pv.SCOPE_PARTY, 1,
         [{"operator": "modify_resource", "params": {"delta": 2}}]),
        ("pas_최후의불꽃", "최후의 불꽃",
         "파티원이 40% 아래로 떨어지면 회복하고 공격력이 오릅니다. 전투당 1회.", 5,
         pv.TRIGGER_ROUND_START, {"hp_below": 0.4}, pv.SCOPE_PARTY, 1,
         [{"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.2}},
          {"operator": "apply_status",
           "params": {"status_id": st.ATTACK_UP, "stacks": 3}}]),
        ("pas_적진교란", "적진 교란", "전투를 시작할 때 적 전체를 약화시킵니다.", 6,
         pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_ENEMIES, 0,
         [{"operator": "apply_status",
           "params": {"status_id": st.DEFENSE_DOWN, "stacks": 2}},
          {"operator": "apply_status",
           "params": {"status_id": st.SPEED_DOWN, "stacks": 1}}]),
    ]
    for (passive_id, name, description, tier, trigger, trigger_params, scope,
         once, effects) in rows:
        db.execute(
            "INSERT OR REPLACE INTO passive_cards (content_version_id, "
            "passive_card_id, name, description, rarity_tier, trigger_event, "
            "trigger_params_json, effects_json, target_scope, once_per_battle, "
            "art_asset, in_gacha_pool, is_retired) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 0)",
            (version, passive_id, name, description, tier, trigger,
             _json(trigger_params), _json(effects), scope, once),
        )


def _seed_achievements(db: Database, version: int) -> None:
    # The ladder shares one counter deliberately, so a player always knows how
    # progress is made. The tutorial boss counts toward `boss_defeated`.
    rows = [
        ("ach_첫보스처치", "첫 보스 처치", "boss_defeated", 1, 100),
        ("ach_보스3회처치", "보스 3회 처치", "boss_defeated", 3, 150),
        ("ach_보스10회처치", "보스 10회 처치", "boss_defeated", 10, 200),
        # 나머지 다섯 카운터에도 목표를 하나씩 준다. 카운터는 이미 엔진이
        # 올리고 있었는데 그것을 보는 업적이 없어서, 진행도가 아무 데도
        # 표시되지 않고 있었다.
        ("ach_런1회클리어", "첫 완주", "run_cleared", 1, 80),
        ("ach_런10회클리어", "열 번의 완주", "run_cleared", 10, 200),
        ("ach_적100처치", "적 100 처치", "enemy_killed", 100, 120),
        ("ach_적500처치", "적 500 처치", "enemy_killed", 500, 250),
        ("ach_장비강화5", "장비 5회 강화", "equipment_tiered", 5, 100),
        ("ach_성급상승3", "성급 3회 상승", "character_starred", 3, 150),
        ("ach_저주정화5", "저주 5회 정화", "curse_removed", 5, 120),
    ]
    for achievement_id, name, counter_key, target, carta in rows:
        db.execute(
            "INSERT OR REPLACE INTO achievements (content_version_id, achievement_id, "
            "name, description, icon_asset, counter_key, target_value, carta_reward, "
            "is_hidden) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 0)",
            (version, achievement_id, name, f"{name} 업적", counter_key, target, carta),
        )


def _seed_research(db: Database, version: int) -> None:
    rows = [
        ("res_파티슬롯3", "파티 슬롯 3", 20000, 10, "ach_보스3회처치",
         {"kind": "party_slot", "value": 3}, 1),
        ("res_패시브슬롯3", "패시브 슬롯 3", 8000, 4, "ach_첫보스처치",
         {"kind": "passive_slot", "value": 3}, 1),
        ("res_패시브슬롯4", "패시브 슬롯 4", 30000, 12, "ach_보스10회처치",
         {"kind": "passive_slot", "value": 4}, 1),
        # 🟡 R-7: 스탯 강화 is deliberately ungated so a coin sink is always open.
        ("res_스탯강화", "스탯 강화", 2000, 0, None, {"kind": "stat_step"}, 10),
    ]
    for node_id, name, coin, wildcards, achievement, effect, steps in rows:
        db.execute(
            "INSERT OR REPLACE INTO research_nodes (content_version_id, node_id, name, "
            "coin_cost, wildcard_cost, required_achievement, effect_json, max_steps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version, node_id, name, coin, wildcards, achievement, _json(effect), steps),
        )


def _seed_banners(db: Database, version: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO banners (content_version_id, banner_id, banner_type, "
        "pickup_type, pickup_target_id, start_at, end_at, pity_scope_id) "
        "VALUES (?, 'banner_standard', 'standard', NULL, NULL, NULL, NULL, "
        "'standard_global')", (version,),
    )
    db.execute(
        "INSERT OR REPLACE INTO banners (content_version_id, banner_id, banner_type, "
        "pickup_type, pickup_target_id, start_at, end_at, pity_scope_id) "
        "VALUES (?, 'banner_limited_aquel', 'limited', 'character', 'char_aquel', "
        "NULL, NULL, 'limited_global')", (version,),
    )


def _seed_equipment(db: Database, version: int) -> None:
    rows = [
        ("eq_수련검", "수련용 검", "무기", "수련자", 0, 4, 0, 0, 3000),
        ("eq_수련갑", "수련용 갑옷", "방어구", "수련자", 12, 0, 3, 0, 3500),
        ("eq_수련부적", "수련용 부적", "악세서리", "수련자", 6, 1, 1, 3, 4000),
        # §8.2의 세트 보너스는 세 부위를 같은 세트로 채웠을 때만 붙는다. 그래서
        # 세트는 언제나 무기/방어구/악세서리 세 줄이 함께 들어온다 — 두 부위만
        # 있는 세트는 영원히 보너스가 붙지 않는 죽은 장비가 된다.
        ("eq_늪검", "늪지 만도", "무기", "늪지기", 0, 7, 0, 0, 7000),
        ("eq_늪갑", "늪지 비늘갑", "방어구", "늪지기", 24, 0, 5, 0, 8000),
        ("eq_늪부적", "늪지 부적", "악세서리", "늪지기", 10, 2, 2, 5, 9000),
        ("eq_심연검", "심연의 검", "무기", "심연", 0, 12, 0, 0, 14000),
        ("eq_심연갑", "심연의 갑옷", "방어구", "심연", 40, 0, 9, 0, 15000),
        ("eq_심연부적", "심연의 인장", "악세서리", "심연", 18, 4, 3, 8, 15000),
    ]
    for def_id, name, slot, set_name, hp, atk, defense, spd, price in rows:
        db.execute(
            "INSERT OR REPLACE INTO equipment_defs (content_version_id, "
            "equipment_def_id, name, slot, set_name, hp_flat, atk_flat, def_flat, "
            "spd_flat, price_coin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, def_id, name, slot, set_name, hp, atk, defense, spd, price),
        )
    # §8.2 full-set only — all 3 slots from the same named set, no 2-piece tier.
    for set_name, bonus in [
        ("수련자", {"atk_flat": 3, "def_flat": 3}),
        ("늪지기", {"hp_flat": 30, "def_flat": 6}),
        ("심연", {"atk_flat": 10, "spd_flat": 6}),
    ]:
        db.execute(
            "INSERT OR REPLACE INTO equipment_sets (content_version_id, set_name, "
            "bonus_json) VALUES (?, ?, ?)", (version, set_name, _json(bonus)),
        )


# =====================================================================
# Entry point
# =====================================================================
def seed_all(db: Database, *, publish_version: bool = True) -> int:
    """Create and populate a content version. Returns its id."""
    version = create_version(db)
    seed_constants(db, version)
    _seed_statuses(db, version)
    _seed_strategies(db, version)
    _seed_cards(db, version)
    _seed_card_upgrades(db, version)
    _seed_characters(db, version)
    _seed_threat_weights(db, version)
    _seed_cursed_cards(db, version)
    _seed_transition_effects(db, version)
    _seed_enemy_actions(db, version)
    _seed_enemies(db, version)
    _seed_encounters(db, version)
    _seed_worlds(db, version)
    _seed_reward_tables(db, version)
    _seed_events(db, version)
    _seed_passives(db, version)
    _seed_achievements(db, version)
    _seed_research(db, version)
    _seed_banners(db, version)
    _seed_equipment(db, version)
    if publish_version:
        publish(db, version)
    return version


# =====================================================================
# §4.6 new-account initialization
# =====================================================================
def create_account(db: Database, user_id: int, content_version_id: int) -> None:
    """§4.6.5 step 2 — ONE local transaction creates the account row, starter
    character, 18-card deck, unlocked-card entries and starting balances.

    Idempotent against a retried first command via UNIQUE(user_id).
    """
    existing = db.one("SELECT user_id FROM accounts WHERE user_id = ?", (user_id,))
    if existing is not None:
        return

    from app.content.balance import Balance

    balance = Balance(db, content_version_id)
    starting_carta = int(balance.get("starting_carta"))

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO accounts (user_id, created_at, updated_at, party_slots, "
            "passive_slots, stat_research_step, wildcards, carta, "
            "first_pull_results_count, first_pull_guarantee_used) "
            "VALUES (?, ?, ?, 1, 2, 0, 0, ?, 0, 0)",
            (user_id, utcnow(), utcnow(), starting_carta),
        )
        conn.execute(
            "INSERT INTO owned_characters (user_id, character_id, star_rank, "
            "acquired_at) VALUES (?, ?, 1, ?)",
            (user_id, STARTER_CHARACTER_ID, utcnow()),
        )
        # §4.6.2 — the 18 starter cards are inserted into the unlocked-card list
        # so 보상 nodes have a legal offer pool from the first run.
        for card_id in (CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE, CARD_STARTER_SKILL):
            conn.execute(
                "INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
                "upgrade_tier, unlocked_at) VALUES (?, ?, 0, ?)",
                (user_id, card_id, utcnow()),
            )
        conn.execute(
            "INSERT OR IGNORE INTO world_unlocks (user_id, world_id, unlocked_at) "
            "VALUES (?, ?, ?)", (user_id, TUTORIAL_WORLD_ID, utcnow()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO gacha_pity (user_id, pity_scope_id, pull_count, "
            "guarantee_pending) VALUES (?, 'standard_global', 0, 0)", (user_id,))
        conn.execute(
            "INSERT OR IGNORE INTO gacha_pity (user_id, pity_scope_id, pull_count, "
            "guarantee_pending) VALUES (?, 'limited_global', 0, 0)", (user_id,))


def starter_deck_cards(balance) -> list[str]:
    """§4.6.2 — exactly 18 cards, composed 6/5/7.

    At 6/5/7 the skill rate in a 3-card draw is 79.8% (`1 − C(11,3)/C(18,3)`);
    v6.1's 8/6/4 gave only 55.4%, which halved real damage output.
    """
    composition = balance.get("starter_deck_composition")
    return (
        [CARD_BASIC_ATTACK] * int(composition["평타"])
        + [CARD_BASIC_DEFENSE] * int(composition["기본_방어"])
        + [CARD_STARTER_SKILL] * int(composition["starter_skill"])
    )
