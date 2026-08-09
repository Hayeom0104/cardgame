"""Seed content — the minimum launch set named in §13.2.

Everything here is *content*, not logic: it is inserted through the same
validation layer the dashboard uses (§10.3), so nothing in this file can
express something the engine cannot execute.

Naming-only 🔴 PENDING items carry their stable internal identifiers:
`starter_001` (P-2) and `run_currency` (P-3). Neither blocks implementation.
"""

from __future__ import annotations

import json

from app.content.balance import seed_constants
from app.content.versioning import create_version, publish
from app.db.connection import Database, utcnow
from app.engine import statuses as st

STARTER_CHARACTER_ID = "starter_001"    # §4.6.1, display name is 🔴 PENDING (P-2)
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
            "persists_through_boss_phase, icon_asset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL)",
            (version, status_id, name, kind, model, clock, cap, duration,
             magnitudes[status_id]),
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
    ]
    for card_id, name, element, cost, category, target_side, tier, effects in cards:
        db.execute(
            "INSERT OR REPLACE INTO cards (content_version_id, card_id, name, element, "
            "cost, category, target_side, rarity_tier, effects_json, art_asset, "
            "is_retired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (version, card_id, name, element, cost, category, target_side, tier,
             _json(effects)),
        )


def _seed_characters(db: Database, version: int) -> None:
    # §4.6.1 — the starter sits OUTSIDE the gacha pool, because a pullable
    # starter produces an ambiguous "duplicate" of a character never pulled.
    db.execute(
        "INSERT OR REPLACE INTO characters (content_version_id, character_id, name, "
        "element, job_role, base_rarity, special_cap, in_gacha_pool, art_asset, "
        "is_retired) VALUES (?, ?, ?, '화', '딜서포트형', 1, 0, 0, NULL, 0)",
        (version, STARTER_CHARACTER_ID, "견습 모험가"),   # 🔴 P-2 placeholder
    )
    # §4.5.2 seed examples — gacha-pool characters.
    for character_id, name, element, role, rarity in [
        ("char_ignis", "이그니스", "화", "공격형", 2),
        ("char_aquel", "아쿠엘", "수", "서포터형", 3),
        ("char_terradon", "테라돈", "지", "방어형", 1),
        ("char_umbra", "움브라", "암", "디버퍼형", 1),
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
        ("world_2", "2세계", 2, 0, 0, 1),
        ("world_3", "3세계", 3, 0, 1, 2),
        ("world_4", "4세계", 4, 0, 2, 3),
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
def _seed_achievements(db: Database, version: int) -> None:
    # The ladder shares one counter deliberately, so a player always knows how
    # progress is made. The tutorial boss counts toward `boss_defeated`.
    rows = [
        ("ach_첫보스처치", "첫 보스 처치", "boss_defeated", 1, 100),
        ("ach_보스3회처치", "보스 3회 처치", "boss_defeated", 3, 150),
        ("ach_보스10회처치", "보스 10회 처치", "boss_defeated", 10, 200),
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
    ]
    for def_id, name, slot, set_name, hp, atk, defense, spd, price in rows:
        db.execute(
            "INSERT OR REPLACE INTO equipment_defs (content_version_id, "
            "equipment_def_id, name, slot, set_name, hp_flat, atk_flat, def_flat, "
            "spd_flat, price_coin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, def_id, name, slot, set_name, hp, atk, defense, spd, price),
        )
    # §8.2 full-set only — all 3 slots from the same named set, no 2-piece tier.
    db.execute(
        "INSERT OR REPLACE INTO equipment_sets (content_version_id, set_name, "
        "bonus_json) VALUES (?, '수련자', ?)",
        (version, _json({"atk_flat": 3, "def_flat": 3})),
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
    _seed_characters(db, version)
    _seed_threat_weights(db, version)
    _seed_cursed_cards(db, version)
    _seed_transition_effects(db, version)
    _seed_enemy_actions(db, version)
    _seed_enemies(db, version)
    _seed_encounters(db, version)
    _seed_worlds(db, version)
    _seed_events(db, version)
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
