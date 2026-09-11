"""Deckout v8.5 — 월드 1 적/보스 기믹 오버레이.

이 파일은 기존 시드의 단순 수치형 월드 1을, 현재 엔진이 이미 지원하는
소환·조건부 행동·상태·텔레그래프·보스 페이즈만으로 기믹형 전투로 바꾼다.

중요한 원칙:
- 외부 게임의 규칙을 그대로 복사하지 않는다. 행동을 보고 대비하게 만드는
  설계 원리만 가져와 Deckout의 턴/텔레그래프 구조에 맞게 다시 만든다.
- 매 publish마다 오너가 만든 최신 콘텐츠를 덮어쓰지 않는다. 오직 v8.5 이전
  기본 시드의 고블린 대족장(450/22/10/100)이 남아 있을 때 한 번만 적용한다.
- 별도 전투 스키마를 늘리지 않는다. 카드 임시 봉인/다단계 게이지는 이후
  공용 엔진 기능으로 분리하고, v8.5 1차 구현은 기존 엔진으로 검증 가능한
  기믹부터 넣는다.
"""

from __future__ import annotations

import json

from app.db.connection import Database
from app.engine import statuses as st

WORLD1_BOSS_ID = "enemy_w1_boss"
WAR_DRUM_READY = "전쟁북_준비"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _upsert_action(db: Database, version_id: int, action_id: str, name: str,
                   category: str, target_side: str, effects: list[dict]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO enemy_actions (content_version_id, action_id, name, "
        "category, target_side, is_basic_attack, effects_json) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (version_id, action_id, name, category, target_side, _json(effects)),
    )


def _legacy_world1_boss(db: Database, version_id: int) -> bool:
    """v8.5 이전 기본 시드인지 판별한다.

    이후 대시보드에서 고친 보스나 이미 v8.5가 적용된 콘텐츠 버전은 건드리지
    않는다. 이 가드는 publish 시점의 자동 오버레이가 콘텐츠 저작을 덮어쓰는
    일을 막는다.
    """
    row = db.one(
        "SELECT hp, atk, def, spd FROM enemies "
        "WHERE content_version_id = ? AND enemy_id = ?",
        (version_id, WORLD1_BOSS_ID),
    )
    if row is None:
        return False
    return (int(row["hp"]), int(row["atk"]), int(row["def"]), int(row["spd"])) == (
        450, 22, 10, 100
    )


def apply_if_legacy_seed(db: Database, version_id: int) -> bool:
    """v8.5 이전 월드 1 기본 시드에만 기믹 오버레이를 적용한다."""
    if not _legacy_world1_boss(db, version_id):
        return False

    with db.tx():
        _seed_status(db, version_id)
        _seed_actions(db, version_id)
        _upgrade_world1_normals(db, version_id)
        _upgrade_world1_boss(db, version_id)
    return True


def _seed_status(db: Database, version_id: int) -> None:
    # 북을 친 턴의 TURN_END에 2→1, 다음 보스 턴의 TURN_END에 1→0이 된다.
    # 따라서 다음 턴 계획을 만들 때만 마커가 살아 있어 '대돌진'을 정확히
    # 한 번 예고한다. 공격력 증가는 별도의 timed modifier로 1라운드만 유지한다.
    db.execute(
        "INSERT OR REPLACE INTO statuses (content_version_id, status_id, name, kind, "
        "model, clock, stack_cap, base_duration, magnitude, cleansable, "
        "persists_through_boss_phase, icon_asset, scope) "
        "VALUES (?, ?, '전쟁북 준비', 'buff', ?, ?, 1, 2, 0, 0, 1, NULL, ?)",
        (version_id, WAR_DRUM_READY, st.COUNTDOWN, st.OWNER_TURN_COUNTDOWN,
         st.UNIVERSAL),
    )


def _seed_actions(db: Database, version_id: int) -> None:
    _upsert_action(
        db, version_id, "act_w1_방패엄호", "방패 엄호", "방어", "ally",
        [{"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.2}}],
    )
    _upsert_action(
        db, version_id, "act_w1_점화술", "점화술", "공격", "enemy",
        [
            {"operator": "deal_damage", "params": {"multiplier": 1.15}},
            {"operator": "apply_status", "params": {"status_id": st.BURN, "stacks": 1}},
        ],
    )
    _upsert_action(
        db, version_id, "act_w1_사냥명령", "사냥 명령", "버프디버프", "ally",
        [
            {"operator": "apply_status", "params": {"status_id": st.ATTACK_UP, "stacks": 1}},
            {"operator": "modify_stat",
             "params": {"stat": "spd", "delta": 8, "is_percent": False,
                        "duration_rounds": 2}},
        ],
    )
    _upsert_action(
        db, version_id, "act_w1_약탈명령", "전리품꾼 호출", "버프디버프", "self",
        [{"operator": "summon_enemy",
          "params": {"enemy_id": "enemy_w1_전리품꾼", "count": 1}}],
    )
    _upsert_action(
        db, version_id, "act_w1_전리품분배", "전리품 분배", "버프디버프", "ally",
        [
            {"operator": "apply_status", "params": {"status_id": st.ATTACK_UP, "stacks": 1}},
            {"operator": "grant_block", "params": {"mode": "multiplier", "value": 0.5}},
        ],
    )
    _upsert_action(
        db, version_id, "act_w1_전쟁북", "전쟁북 고동", "버프디버프", "self",
        [
            {"operator": "apply_status", "params": {"status_id": WAR_DRUM_READY}},
            {"operator": "modify_stat",
             "params": {"stat": "atk", "delta": 25, "is_percent": True,
                        "duration_rounds": 1}},
            {"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.0}},
        ],
    )
    _upsert_action(
        db, version_id, "act_w1_대돌진", "대돌진", "공격", "enemy",
        [{"operator": "deal_damage", "params": {"multiplier": 1.8}}],
    )


def _upgrade_world1_normals(db: Database, version_id: int) -> None:
    # 일반 고블린: 마지막 한 마리가 되면 잠깐 폭주한다. 단순 평타 몹에서
    # '마지막에 남겨도 되는가'를 생각하게 만드는 입문용 처치 순서 기믹.
    goblin_rules = [
        {"priority": 4, "condition": {"op": "own_side_count_below", "value": 2},
         "action_id": "act_필사반격", "weight": 1.0, "cooldown_turns": 3},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 3.0, "cooldown_turns": 0},
        {"priority": 20, "condition": None, "action_id": "act_강타",
         "weight": 0.7, "cooldown_turns": 1},
    ]
    db.execute(
        "UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_고블린'",
        (_json(goblin_rules), version_id),
    )

    # 방패병: 체력이 빠진 아군에게 방어를 넘겨준다. 플레이어가 '딜러 먼저'가
    # 아니라 '보호하는 놈 먼저'라는 타겟 우선순위를 배우게 한다.
    shield_rules = [
        {"priority": 4, "condition": {"op": "any_ally_hp_below", "value": 0.65},
         "action_id": "act_w1_방패엄호", "weight": 1.0, "cooldown_turns": 1},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 3.0, "cooldown_turns": 0},
        {"priority": 20, "condition": None, "action_id": "act_강타",
         "weight": 0.5, "cooldown_turns": 1},
    ]
    db.execute(
        "UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_방패병'",
        (_json(shield_rules), version_id),
    )

    # 주술사: 화상만 뿌리는 대신, 이미 디버프가 붙은 파티를 보면 점화 공격을
    # 섞는다. 상태이상 아이콘과 적 텔레그래프를 함께 보게 만드는 역할.
    shaman_rules = [
        {"priority": 4, "condition": {"op": "target_has_status", "value": st.BURN},
         "action_id": "act_w1_점화술", "weight": 1.0, "cooldown_turns": 1},
        {"priority": 6, "condition": {"op": "always"},
         "action_id": "act_화상부여", "weight": 1.0, "cooldown_turns": 2},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 3.0, "cooldown_turns": 0},
    ]
    db.execute(
        "UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_주술사'",
        (_json(shaman_rules), version_id),
    )

    # 늑대조련사: 공격력이 아니라 '다음에 누가 빨라질지'를 흔든다. 속도는
    # 다음 라운드 순서 스냅샷부터 적용되므로 텔레그래프와 정확히 맞물린다.
    tamer_rules = [
        {"priority": 5, "condition": {"op": "always"},
         "action_id": "act_w1_사냥명령", "weight": 1.0, "cooldown_turns": 2},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 3.0, "cooldown_turns": 0},
    ]
    db.execute(
        "UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_늑대조련사'",
        (_json(tamer_rules), version_id),
    )

    # 돌격병은 기존의 체력 50% 이하 폭주를 유지한다. 월드 1 전체가 모두 새
    # 규칙을 들고 있으면 학습량이 과해지므로, 하나는 단순한 '처형 우선' 몹으로 둔다.


def _upgrade_world1_boss(db: Database, version_id: int) -> None:
    loot_rules = [
        {"priority": 5, "condition": {"op": "always"},
         "action_id": "act_w1_전리품분배", "weight": 1.0, "cooldown_turns": 1},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 2.0, "cooldown_turns": 0},
    ]
    db.execute(
        "INSERT OR REPLACE INTO enemies (content_version_id, enemy_id, name, tier, "
        "element, role, hp, atk, def, spd, strategy_override, action_rules_json, "
        "art_asset, is_retired) VALUES (?, 'enemy_w1_전리품꾼', '고블린 전리품꾼', "
        "'일반', '지', '서포터형', 38, 6, 2, 84, 'strat_lowest_hp_pct', ?, NULL, 0)",
        (version_id, _json(loot_rules)),
    )

    boss_rules = [
        # 1페이즈: 2라운드부터 한 번만 전리품꾼을 부른다. cooldown 99라 같은
        # 전투에서 재소환되지 않는다. 소환수를 무시하면 계속 버프가 쌓인다.
        {"priority": 2, "condition": {"op": "round_number_gte", "value": 2},
         "action_id": "act_w1_약탈명령", "weight": 1.0, "cooldown_turns": 99,
         "max_phase": 1},
        # 2페이즈: 북 준비 마커가 보이면 다음 행동은 반드시 대돌진.
        {"priority": 3, "condition": {"op": "self_has_status", "value": WAR_DRUM_READY},
         "action_id": "act_w1_대돌진", "weight": 1.0, "cooldown_turns": 0,
         "min_phase": 2},
        # 마커가 없으면 북을 친다. 즉 2페이즈는 [북 예고 → 대돌진]이 반복된다.
        {"priority": 4, "condition": {"op": "phase_is", "value": 2},
         "action_id": "act_w1_전쟁북", "weight": 1.0, "cooldown_turns": 0,
         "min_phase": 2},
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 3.0, "cooldown_turns": 0},
        {"priority": 20, "condition": None, "action_id": "act_강타",
         "weight": 1.0, "cooldown_turns": 1, "max_phase": 1},
    ]
    db.execute(
        "UPDATE enemies SET hp = 450, atk = 20, def = 8, spd = 96, "
        "action_rules_json = ? WHERE content_version_id = ? AND enemy_id = ?",
        (_json(boss_rules), version_id, WORLD1_BOSS_ID),
    )

    # 기존의 50% 무적 진입은 제거한다. 첫 본편 보스는 '한 턴 아무것도 못 함'이
    # 아니라 앞으로 올 공격을 보고 대비하는 법을 가르쳐야 한다.
    db.execute(
        "INSERT OR REPLACE INTO transition_effects (content_version_id, "
        "transition_effect_id, name, effects_json) "
        "VALUES (?, 'trans_w1_전쟁개시', '전쟁 개시', ?)",
        (version_id, _json([
            {"operator": "grant_block", "params": {"mode": "multiplier", "value": 1.0}}
        ])),
    )
    db.execute(
        "UPDATE boss_phases SET hp_threshold_pct = 0.55, effect_ids_json = ? "
        "WHERE content_version_id = ? AND enemy_id = ? AND phase_index = 2",
        (_json(["trans_w1_전쟁개시"]), version_id, WORLD1_BOSS_ID),
    )
