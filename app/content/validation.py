"""§10.5 — content validation.

Every dashboard save and every service startup runs the same pass. A violation
blocks the save, or fails startup **CLOSED**. Content is never partially loaded.

The dashboard must never persist content the engine cannot execute (§10.3), so
this is the single validation layer both paths go through.
"""

from __future__ import annotations

import json
import math

from app.content import operators as ops
from app.content.operators import ValidationError
from app.db.connection import Database
from app.engine import statuses as st
from app.engine.stats import ALL_ELEMENTS

VALID_TARGET_SIDES = frozenset({"enemy", "ally", "self", "all"})
VALID_STATUS_MODELS = frozenset({st.COUNTDOWN, st.STACK_DURATION, st.STACK_DECAY})
VALID_STATUS_CLOCKS = frozenset({st.TURN_START_TRIGGER, st.OWNER_TURN_COUNTDOWN})


def validate_version(db: Database, version_id: int) -> None:
    """Run every check. Raises ValidationError on the first violation."""
    _validate_statuses(db, version_id)
    _validate_strategies(db, version_id)
    _validate_threat_grid(db, version_id)
    _validate_cards(db, version_id)
    _validate_cursed_cards(db, version_id)
    _validate_enemy_actions(db, version_id)
    _validate_enemies(db, version_id)
    _validate_transition_effects(db, version_id)
    _validate_boss_phases(db, version_id)
    _validate_events(db, version_id)
    _validate_encounters(db, version_id)
    _validate_world_coverage(db, version_id)
    _validate_research(db, version_id)
    _validate_card_upgrades(db, version_id)
    _validate_passives(db, version_id)
    _validate_constants(db, version_id)


def _validate_passives(db: Database, version_id: int) -> None:
    """§6 패시브 카드.

    패시브에는 시전자가 없다 (`engine/passives.py` 참고). 시전자의 능력치나
    손패에 기대는 연산자는 전투 중에 조용히 아무 일도 하지 않거나 엉뚱한
    대상을 잡으므로, 저장하는 시점에 막는다.
    """
    from app.engine import passives as pv

    scopes = _status_scopes(db, version_id)
    for row in db.query("SELECT * FROM passive_cards WHERE content_version_id = ?",
                        (version_id,)):
        label = f"passive {row['passive_card_id']!r}"
        if row["trigger_event"] not in pv.TRIGGERS:
            raise ValidationError(
                f"{label}: trigger_event {row['trigger_event']!r} 은(는) "
                f"{list(pv.TRIGGERS)} 중 하나여야 합니다 (§6)")
        if row["target_scope"] not in pv.SCOPES:
            raise ValidationError(
                f"{label}: target_scope {row['target_scope']!r} 은(는) "
                f"{list(pv.SCOPES)} 중 하나여야 합니다")
        if not 1 <= int(row["rarity_tier"]) <= 6:
            raise ValidationError(f"{label}: rarity_tier must be 1-6 (§5.6)")
        if int(row["once_per_battle"]) < 0:
            raise ValidationError(f"{label}: once_per_battle 은 0 이상이어야 합니다")
        try:
            json.loads(row["trigger_params_json"] or "{}")
        except json.JSONDecodeError as error:
            raise ValidationError(f"{label}: trigger_params_json 이 JSON 이 아닙니다") from error

        effects = _effects(row["effects_json"])
        if not effects:
            raise ValidationError(f"{label}: 효과가 비어 있습니다")
        try:
            ops.validate_effect_list(effects, ops.CTX_PASSIVE)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error
        for index, entry in enumerate(effects):
            if entry.get("operator") not in pv.ALLOWED_OPERATORS:
                raise ValidationError(
                    f"{label}: effect[{index}] 의 {entry.get('operator')!r} 은(는) "
                    "패시브에 쓸 수 없습니다 — 패시브는 시전자도 손패도 없이 "
                    f"발동합니다. 쓸 수 있는 것: {sorted(pv.ALLOWED_OPERATORS)}")
        # 적을 대상으로 하는 패시브는 §2.5.1a 의 적 풀에 속한다.
        if row["target_scope"] == pv.SCOPE_ENEMIES:
            _reject_scoped_statuses(effects, scopes, st.PLAYER_ONLY, label)
        else:
            _reject_scoped_statuses(effects, scopes, st.ENEMY_ONLY, label)


def _validate_constants(db: Database, version_id: int) -> None:
    """§15 상수들이 서로 어긋나지 않는지 (§10.5).

    이 검사가 없으면 관리 대시보드에서 확률의 합이 1이 아닌 표를 발행할 수
    있고, 그 값은 아무 소리 없이 게임을 망가뜨린다. 파일을 고치는 사람은
    `tests/test_config.py` 가 잡아 주지만, 대시보드로 고치는 사람에게는
    발행 시점의 이 검사가 유일한 안전망이다.
    """
    balance = _balance(db, version_id)

    def get(key):
        try:
            return balance.get(key)
        except KeyError as error:
            raise ValidationError(f"§15 상수 {key!r} 이(가) 없습니다") from error

    for key in ("gacha_base_rates", "reward_rarity_weights"):
        total = sum(float(value) for value in get(key).values())
        if abs(total - 1.0) > 1e-6:
            raise ValidationError(
                f"{key}: 확률의 합이 {total}입니다. 1이어야 합니다 (§15.4)")

    deck_size = int(get("base_deck_size"))
    composition = sum(int(value) for value in get("starter_deck_composition").values())
    if composition != deck_size:
        raise ValidationError(
            f"starter_deck_composition의 합 {composition}이(가) "
            f"base_deck_size {deck_size}와(과)다릅니다 — 덱이 정확히 채워지지 "
            "않습니다 (§4.6.2)")

    node_count = int(get("map_node_count"))
    quota = sum(int(value) for value in get("map_node_quota").values())
    structure = sum(int(value) for value in get("map_depth_structure"))
    if quota != node_count or structure != node_count:
        raise ValidationError(
            f"지도 칸 수가 어긋납니다: map_node_count {node_count}, "
            f"map_node_quota 합 {quota}, map_depth_structure 합 {structure} "
            "(§15.7)")

    threshold = int(get("card_upgrade_wildcard_from_tier"))
    for tier, cost in get("card_upgrade_costs").items():
        wildcards = int(cost["wildcards"])
        if int(tier) < threshold and wildcards:
            raise ValidationError(
                f"card_upgrade_costs T{tier}: 와일드카드는 "
                f"{threshold}단계부터 듭니다 (§5.8.2)")
        if int(tier) >= threshold and wildcards <= 0:
            raise ValidationError(
                f"card_upgrade_costs T{tier}: 와일드카드가 들어야 합니다 (§5.8.2)")

    pool = get("resource_pool_by_party_size")
    max_cost = int(get("card_cost_max"))
    if max_cost > max(int(value) for value in pool.values()):
        raise ValidationError(
            f"card_cost_max {max_cost}이(가) 한 턴 자원보다 큽니다 — 그 비용의 "
            "카드는 영영 낼 수 없습니다 (§2.3)")

    # 튜토리얼 보상이 부족하면 처음 하는 사람이 본편에 들어갈 수 없다 (§4.1).
    if int(get("tutorial_reward_carta")) < int(get("gacha_cost_single")):
        raise ValidationError(
            "tutorial_reward_carta가 뽑기 1회 비용보다 적습니다 — 튜토리얼을 "
            "마친 계정이 두 번째 캐릭터를 얻을 수 없어 본편 진입이 막힙니다")
    if int(get("tutorial_reward_party_slot")) < 2:
        raise ValidationError(
            "tutorial_reward_party_slot이 2 미만입니다 — 본편은 파티 2명부터 "
            "들어갈 수 있습니다")

    covered = {tier for band in get("gacha_band_rarity_tiers").values()
               for tier in band}
    missing = {1, 2, 3, 4, 5, 6} - covered
    if missing:
        raise ValidationError(
            f"gacha_band_rarity_tiers에 희귀도 {sorted(missing)}이(가) 어느 "
            "등급에도 없습니다 — 그 희귀도의 카드는 영영 뽑히지 않습니다")

    share = float(get("gacha_passive_share_of_cards"))
    if not 0.0 <= share <= 1.0:
        raise ValidationError(
            f"gacha_passive_share_of_cards {share}은(는) 0과 1 사이여야 합니다")

    # 출석 (§13.2)
    reset_hour = int(get("daily_reset_hour_kst"))
    if not 0 <= reset_hour <= 23:
        raise ValidationError(
            f"daily_reset_hour_kst {reset_hour}은(는) 0~23 이어야 합니다")
    if int(get("daily_streak_grace_days")) < 0:
        raise ValidationError("daily_streak_grace_days는 0 이상이어야 합니다")
    rewards = get("daily_rewards")
    if not rewards:
        raise ValidationError(
            "daily_rewards가 비어 있습니다 — 출석 주기의 길이가 0이 되어 "
            "며칠째인지 정할 수 없습니다")
    days = sorted(int(day) for day in rewards)
    if days != list(range(1, len(days) + 1)):
        raise ValidationError(
            f"daily_rewards의 날짜가 1부터 연속이 아닙니다: {days} — 주기를 "
            "도는 도중 빈 날이 생깁니다")

    table = get("equipment_drop_tier_by_depth")
    deepest = len(get("map_depth_structure")) + 1
    if not table or int(table[-1]["max_depth"]) < deepest:
        raise ValidationError(
            "equipment_drop_tier_by_depth의 마지막 줄이 최대 깊이를 덮지 "
            "못합니다 — 그 깊이에서 장비 등급이 정해지지 않습니다")


def _effects(raw: str) -> list[dict]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValidationError(f"effect list is not valid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise ValidationError("effect list must be a JSON array")
    return parsed


def _validate_statuses(db: Database, version_id: int) -> None:
    """Every status declares exactly one clock (§2.5.1)."""
    for row in db.query("SELECT * FROM statuses WHERE content_version_id = ?",
                        (version_id,)):
        label = f"status {row['status_id']!r}"
        if row["clock"] not in VALID_STATUS_CLOCKS:
            raise ValidationError(f"{label}: clock {row['clock']!r} is not one of "
                                  f"{sorted(VALID_STATUS_CLOCKS)}")
        if row["model"] not in VALID_STATUS_MODELS:
            raise ValidationError(f"{label}: model {row['model']!r} is not one of "
                                  f"{sorted(VALID_STATUS_MODELS)}")
        if row["kind"] not in ("buff", "debuff"):
            raise ValidationError(f"{label}: kind must be buff or debuff")

        # stack_decay has NO duration — the stack count IS the lifetime (C-04).
        if row["model"] == st.STACK_DECAY and row["base_duration"] is not None:
            raise ValidationError(
                f"{label}: model stack_decay must not declare base_duration — the "
                "stack count is the lifetime"
            )
        if row["model"] != st.STACK_DECAY and row["base_duration"] is None:
            raise ValidationError(f"{label}: model {row['model']!r} requires base_duration")
        # countdown has no intensity.
        if row["model"] == st.COUNTDOWN and row["stack_cap"] is not None:
            raise ValidationError(f"{label}: countdown statuses have no stack_cap")

        # §2.5.1a [v6.4] — scope is declared once, at authoring time.
        if row["scope"] not in st.SCOPES:
            raise ValidationError(
                f"{label}: scope {row['scope']!r} is not one of {sorted(st.SCOPES)}")


def _status_scopes(db: Database, version_id: int) -> dict[str, str]:
    return {row["status_id"]: row["scope"] for row in db.query(
        "SELECT status_id, scope FROM statuses WHERE content_version_id = ?",
        (version_id,))}


def _reject_scoped_statuses(effects: list[dict], scopes: dict[str, str],
                            forbidden: str, label: str) -> None:
    """§2.5.1a — a status may only appear in the pools its scope allows."""
    for index, entry in enumerate(effects):
        if entry.get("operator") != "apply_status":
            continue
        status_id = (entry.get("params") or {}).get("status_id")
        if scopes.get(status_id) == forbidden:
            raise ValidationError(
                f"{label}: effect[{index}] applies {status_id!r}, which is "
                f"scoped {forbidden!r} and is excluded from this content pool "
                "(§2.5.1a)")


def _validate_strategies(db: Database, version_id: int) -> None:
    ids = {row["strategy_id"] for row in db.query(
        "SELECT strategy_id FROM targeting_strategies WHERE content_version_id = ?",
        (version_id,))}
    for row in db.query("SELECT * FROM targeting_strategies WHERE content_version_id = ?",
                        (version_id,)):
        label = f"targeting strategy {row['strategy_id']!r}"
        if row["selector_operator"] not in ops.SELECTOR_OPERATORS:
            raise ValidationError(
                f"{label}: selector {row['selector_operator']!r} is not in the closed "
                "set (§10.4.4)")
        if row["selector_operator"] == "highest_score":
            if row["scoring_expression"] not in ops.SCORING_EXPRESSIONS:
                raise ValidationError(
                    f"{label}: highest_score requires a supported scoring_expression")
        if row["tie_breaker"] != "registration_order":
            raise ValidationError(f"{label}: tie_breaker must be registration_order")
        if row["fallback_strategy_id"] and row["fallback_strategy_id"] not in ids:
            raise ValidationError(f"{label}: fallback {row['fallback_strategy_id']!r} "
                                  "does not resolve")

    for row in db.query("SELECT * FROM enemy_roles WHERE content_version_id = ?",
                        (version_id,)):
        if row["default_strategy_id"] not in ids:
            raise ValidationError(
                f"enemy role {row['enemy_role_id']!r}: default strategy "
                f"{row['default_strategy_id']!r} does not resolve")


def _validate_threat_grid(db: Database, version_id: int) -> None:
    """Every threat-weight (row, column) pair is populated (§15.3).

    A new role needs a new row AND column; a partially populated grid is
    rejected.
    """
    enemy_roles = {row["enemy_role_id"] for row in db.query(
        "SELECT enemy_role_id FROM enemy_roles WHERE content_version_id = ?",
        (version_id,))}
    ally_roles = {row["job_role"] for row in db.query(
        "SELECT DISTINCT job_role FROM characters WHERE content_version_id = ?",
        (version_id,))}
    populated = {(row["enemy_role_id"], row["ally_role_id"]) for row in db.query(
        "SELECT enemy_role_id, ally_role_id FROM threat_weights "
        "WHERE content_version_id = ?", (version_id,))}

    missing = [
        (enemy_role, ally_role)
        for enemy_role in sorted(enemy_roles)
        for ally_role in sorted(ally_roles)
        if (enemy_role, ally_role) not in populated
    ]
    if missing:
        raise ValidationError(
            f"threat_weights grid is partially populated; missing {missing}")


def _validate_cards(db: Database, version_id: int) -> None:
    """Every card has a valid element (one of 7) and a target_side."""
    for row in db.query("SELECT * FROM cards WHERE content_version_id = ?",
                        (version_id,)):
        label = f"card {row['card_id']!r}"
        # `modify_cost` only means something to the §5.8 overlay, which folds it
        # into the card's `cost` field. Left on a card's own effect list it would
        # reach the executor, which has no handler for it, and blow up mid-turn.
        for index, entry in enumerate(_effects(row["effects_json"])):
            if entry.get("operator") == "modify_cost":
                raise ValidationError(
                    f"{label}: effect[{index}] uses 'modify_cost', which is only "
                    "legal inside a card upgrade overlay (§5.8.3)")
        if row["element"] not in ALL_ELEMENTS:
            raise ValidationError(f"{label}: element {row['element']!r} is not one of "
                                  f"{list(ALL_ELEMENTS)}")
        if row["target_side"] not in VALID_TARGET_SIDES:
            raise ValidationError(f"{label}: target_side {row['target_side']!r} is invalid")
        if not 1 <= int(row["rarity_tier"]) <= 6:
            raise ValidationError(f"{label}: rarity_tier must be 1-6 (§5.6)")
        try:
            ops.validate_effect_list(_effects(row["effects_json"]),
                                     ops.CTX_BATTLE_CARD)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error


def _validate_cursed_cards(db: Database, version_id: int) -> None:
    scopes = _status_scopes(db, version_id)
    for row in db.query("SELECT * FROM cursed_cards WHERE content_version_id = ?",
                        (version_id,)):
        label = f"cursed card {row['cursed_card_id']!r}"
        effects = _effects(row["penalty_json"])
        try:
            ops.validate_effect_list(effects, ops.CTX_CURSED_CARD)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error
        # 저주받은 카드는 플레이어에게 가해지는 콘텐츠이므로 §2.5.1a의 적 풀에
        # 속한다: player_only 상태는 여기 나타날 수 없다.
        _reject_scoped_statuses(effects, scopes, st.PLAYER_ONLY, label)


def _validate_enemy_actions(db: Database, version_id: int) -> None:
    scopes = _status_scopes(db, version_id)
    for row in db.query("SELECT * FROM enemy_actions WHERE content_version_id = ?",
                        (version_id,)):
        label = f"enemy action {row['action_id']!r}"
        if row["target_side"] not in VALID_TARGET_SIDES:
            raise ValidationError(f"{label}: target_side {row['target_side']!r} is invalid")
        effects = _effects(row["effects_json"])
        try:
            ops.validate_effect_list(effects, ops.CTX_ENEMY_ACTION)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error
        _reject_scoped_statuses(effects, scopes, st.PLAYER_ONLY, label)


def _validate_enemies(db: Database, version_id: int) -> None:
    """Every enemy has at least one 기본 공격 action rule (§2.8.5 step 4)."""
    actions = {row["action_id"]: row for row in db.query(
        "SELECT * FROM enemy_actions WHERE content_version_id = ?", (version_id,))}
    roles = {row["enemy_role_id"] for row in db.query(
        "SELECT enemy_role_id FROM enemy_roles WHERE content_version_id = ?",
        (version_id,))}

    for row in db.query("SELECT * FROM enemies WHERE content_version_id = ?",
                        (version_id,)):
        label = f"enemy {row['enemy_id']!r}"
        if row["role"] not in roles:
            raise ValidationError(f"{label}: role {row['role']!r} does not resolve")
        rules = json.loads(row["action_rules_json"])
        if not rules:
            raise ValidationError(f"{label}: has no action rules")

        has_basic = False
        for rule in rules:
            action_id = rule.get("action_id")
            if action_id not in actions:
                raise ValidationError(f"{label}: action {action_id!r} does not resolve")
            if actions[action_id]["is_basic_attack"]:
                has_basic = True
            if rule.get("condition") is not None:
                op = rule["condition"].get("op")
                if op not in ops.CONDITION_OPERATORS:
                    raise ValidationError(
                        f"{label}: condition operator {op!r} is not in the closed set")
            # A summon rule with cooldown_turns = 0 is REJECTED outright —
            # unbounded by construction (C-09).
            if _rule_summons(actions[action_id]) and int(rule.get("cooldown_turns", 0)) <= 0:
                raise ValidationError(
                    f"{label}: action {action_id!r} summons with cooldown_turns = 0, "
                    "which is unbounded by construction")
        if not has_basic:
            raise ValidationError(f"{label}: defines no 기본 공격 action rule")


def _rule_summons(action_row) -> bool:
    return any(entry.get("operator") == "summon_enemy"
               for entry in json.loads(action_row["effects_json"]))


def _validate_transition_effects(db: Database, version_id: int) -> None:
    for row in db.query("SELECT * FROM transition_effects WHERE content_version_id = ?",
                        (version_id,)):
        try:
            ops.validate_effect_list(_effects(row["effects_json"]),
                                     ops.CTX_TRANSITION_EFFECT)
        except ValidationError as error:
            raise ValidationError(
                f"transition effect {row['transition_effect_id']!r}: {error}") from error


def _validate_boss_phases(db: Database, version_id: int) -> None:
    """Every boss phase list is ordered and non-overlapping."""
    effects = {row["transition_effect_id"] for row in db.query(
        "SELECT transition_effect_id FROM transition_effects WHERE content_version_id = ?",
        (version_id,))}
    by_enemy: dict[str, list] = {}
    for row in db.query("SELECT * FROM boss_phases WHERE content_version_id = ? "
                        "ORDER BY enemy_id, phase_index", (version_id,)):
        by_enemy.setdefault(row["enemy_id"], []).append(row)
        for effect_id in json.loads(row["effect_ids_json"]):
            if effect_id not in effects:
                raise ValidationError(
                    f"boss phase {row['boss_phase_id']!r}: transition effect "
                    f"{effect_id!r} does not resolve")

    for enemy_id, phases in by_enemy.items():
        indices = [int(phase["phase_index"]) for phase in phases]
        if indices != sorted(set(indices)):
            raise ValidationError(
                f"boss {enemy_id!r}: phase indices must be ordered and unique")
        thresholds = [float(phase["hp_threshold_pct"]) for phase in phases]
        if thresholds != sorted(thresholds, reverse=True):
            raise ValidationError(
                f"boss {enemy_id!r}: hp thresholds must descend with phase index")


def _validate_events(db: Database, version_id: int) -> None:
    for row in db.query("SELECT * FROM events WHERE content_version_id = ?",
                        (version_id,)):
        label = f"event {row['event_id']!r}"
        for branch in json.loads(row["branches_json"]):
            try:
                ops.validate_effect_list(branch.get("effects", []), ops.CTX_EVENT)
            except ValidationError as error:
                raise ValidationError(
                    f"{label} branch {branch.get('label')!r}: {error}") from error


def _validate_world_coverage(db: Database, version_id: int) -> None:
    """모든 월드가 전투 칸과 보스 칸에 쓸 조우를 갖고 있는가.

    지도는 언제나 전투 칸과 보스 칸을 만들고(§15.7의 quota), 그 칸에 도달하면
    `nodes._pick_encounter` 가 그 월드의 조우를 찾는다. 하나도 없으면 런이
    그 자리에서 죽는다 — 그것도 플레이어가 몇 분을 들여 거기까지 간 뒤에.

    엔진은 이 상황에서 "§10.5가 막았어야 한다"는 오류를 냈지만, §10.5에는
    정작 그 검사가 없었다. 여기가 그 검사다.
    """
    worlds = db.query(
        "SELECT world_id, name FROM worlds WHERE content_version_id = ? "
        "ORDER BY sequence_index", (version_id,))
    authored: dict[tuple[str, str], int] = {}
    for row in db.query(
            "SELECT world_id, kind, COUNT(*) AS n FROM encounters "
            "WHERE content_version_id = ? GROUP BY world_id, kind", (version_id,)):
        authored[(row["world_id"], row["kind"])] = int(row["n"])

    for world in worlds:
        for kind in ("normal", "boss"):
            if not authored.get((world["world_id"], kind)):
                raise ValidationError(
                    f"world {world['world_id']!r}({world['name']}) 에 {kind} "
                    "조우가 하나도 없습니다 — 그 월드의 런은 해당 칸에서 "
                    "멈춥니다 (§3.1)")


def _validate_encounters(db: Database, version_id: int) -> None:
    """Every encounter has 1-8 enemies AND passes the summon bound check."""
    balance = _balance(db, version_id)
    cap = int(balance.get("max_enemies_per_encounter", 8))
    budget = int(balance.get("encounter_round_budget", 12))

    enemies = {row["enemy_id"]: row for row in db.query(
        "SELECT * FROM enemies WHERE content_version_id = ?", (version_id,))}
    actions = {row["action_id"]: row for row in db.query(
        "SELECT * FROM enemy_actions WHERE content_version_id = ?", (version_id,))}

    for row in db.query("SELECT * FROM encounters WHERE content_version_id = ?",
                        (version_id,)):
        label = f"encounter {row['encounter_id']!r}"
        units = json.loads(row["units_json"])
        if not 1 <= len(units) <= cap:
            raise ValidationError(f"{label}: must hold 1-{cap} enemies, has {len(units)}")

        bound = len(units)
        for entry in units:
            enemy = enemies.get(entry["enemy_id"])
            if enemy is None:
                raise ValidationError(f"{label}: enemy {entry['enemy_id']!r} does not resolve")
            bound += _summon_bound(enemy, actions, budget)

        # Deliberately pessimistic: it may reject an encounter that would in
        # practice stay under the cap. That is the correct direction — the
        # alternative is the runtime cap silently swallowing summons the
        # designer expected to fire.
        if bound > cap:
            raise ValidationError(
                f"{label}: conservative summon bound is {bound}, above the cap of {cap}")


def _summon_bound(enemy_row, actions: dict, budget: int) -> int:
    """Σ over every action rule containing summon_enemy, of
    `count × ceil(ENCOUNTER_ROUND_BUDGET / (cooldown_turns + 1))`."""
    total = 0
    for rule in json.loads(enemy_row["action_rules_json"]):
        action = actions.get(rule.get("action_id"))
        if action is None:
            continue
        for entry in json.loads(action["effects_json"]):
            if entry.get("operator") != "summon_enemy":
                continue
            count = int(entry.get("params", {}).get("count", 1))
            cooldown = int(rule.get("cooldown_turns", 0))
            total += count * math.ceil(budget / (cooldown + 1))
    return total


def _validate_research(db: Database, version_id: int) -> None:
    """Every achievement referenced by a research node exists (§20)."""
    achievements = {row["achievement_id"] for row in db.query(
        "SELECT achievement_id FROM achievements WHERE content_version_id = ?",
        (version_id,))}
    for row in db.query("SELECT * FROM research_nodes WHERE content_version_id = ?",
                        (version_id,)):
        required = row["required_achievement"]
        if required and required not in achievements:
            raise ValidationError(
                f"research node {row['node_id']!r}: required achievement "
                f"{required!r} does not resolve")


def _validate_card_upgrades(db: Database, version_id: int) -> None:
    """§5.8 [v6.4] — 카드 업그레이드 전이가 따라야 할 규칙.

    §5.8.5는 티어별 비용과 효과 *선택*을 콘텐츠 작업으로 남겨 두지만, 규칙
    자체는 여기서 강제된다: 티어 범위, 와일드카드 발생 지점, 가파른 곡선,
    그리고 전이별 능력 추가 게이트.
    """
    from app.engine import card_upgrades as cu

    limits = cu.rules_for(db, version_id)
    scopes = _status_scopes(db, version_id)
    cards = {row["card_id"] for row in db.query(
        "SELECT card_id FROM cards WHERE content_version_id = ?", (version_id,))}

    by_card: dict[str, list] = {}
    for row in db.query(
        "SELECT * FROM card_upgrades WHERE content_version_id = ? "
        "ORDER BY card_id, target_tier", (version_id,),
    ):
        label = f"card upgrade {row['card_id']!r} → T{row['target_tier']}"
        if row["card_id"] not in cards:
            raise ValidationError(f"{label}: card does not resolve")

        tier = int(row["target_tier"])
        if not cu.MIN_TIER < tier <= limits.max_tier:
            raise ValidationError(
                f"{label}: target_tier must be 1..{limits.max_tier} (§5.8.1)")

        # §5.8.2 — 와일드카드는 2→3 전이부터 든다.
        wildcards = int(row["wildcard_cost"])
        if tier < limits.wildcard_from_tier and wildcards:
            raise ValidationError(
                f"{label}: 와일드카드 is only spent from the 2→3 transition "
                "onward (§5.8.2)")
        if tier >= limits.wildcard_from_tier and wildcards <= 0:
            raise ValidationError(
                f"{label}: transitions from 2→3 onward must cost 와일드카드 "
                "(§5.8.2)")
        if int(row["fragment_cost"]) <= 0 or int(row["coin_cost"]) <= 0:
            raise ValidationError(
                f"{label}: every transition costs that card's 조각 and 코인")

        effects = _effects(row["effects_json"])
        try:
            ops.validate_effect_list(effects, ops.CTX_CARD_UPGRADE)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error

        # §5.8.3 — 전이가 담을 수 있는 것은 숫자 변경과 능력 추가뿐이다.
        # 그 밖의 연산자(draw_cards, summon_enemy …)를 오버레이에 넣으면
        # 전이별 게이트를 통째로 우회하게 된다.
        for index, entry in enumerate(effects):
            operator = entry["operator"]
            if (operator not in cu.NUMERIC_OPERATORS
                    and operator != "apply_status"):
                raise ValidationError(
                    f"{label}: effect[{index}] uses {operator!r}; a transition "
                    "carries a numeric change or an ability addition only "
                    "(§5.8.3)")

        # §5.8.3 — 능력 추가는 전이와 상태 scope로 게이트된다.
        allowed = limits.allowed_scopes(tier)
        for added in cu.added_status_operators(effects):
            status_id = (added.get("params") or {}).get("status_id")
            scope = scopes.get(status_id)
            if scope is None:
                raise ValidationError(
                    f"{label}: applies {status_id!r}, which is not defined")
            if not allowed:
                raise ValidationError(
                    f"{label}: the 0→1 transition takes numeric changes only — "
                    "no ability addition (§5.8.3)")
            if scope not in allowed:
                raise ValidationError(
                    f"{label}: {status_id!r} is scoped {scope!r}, but this "
                    f"transition admits only {list(allowed)} (§5.8.3)")

            # §5.8.3 — 지속시간은 4턴까지이며 상태 자신의 base_duration에도 묶인다.
            override = (added.get("params") or {}).get("duration_override")
            if override is not None:
                if override > limits.max_applied_duration:
                    raise ValidationError(
                        f"{label}: applied duration {override} exceeds the "
                        f"{limits.max_applied_duration}-turn cap (§5.8.3)")
                base = db.one(
                    "SELECT base_duration FROM statuses WHERE content_version_id = ? "
                    "AND status_id = ?", (version_id, status_id))
                if (base and base["base_duration"] is not None
                        and override > int(base["base_duration"])):
                    raise ValidationError(
                        f"{label}: applied duration {override} exceeds "
                        f"{status_id!r}'s own base_duration (§5.8.3)")

        by_card.setdefault(row["card_id"], []).append(row)

    for card_id, rows in by_card.items():
        tiers = [int(row["target_tier"]) for row in rows]
        if tiers != list(range(1, len(tiers) + 1)):
            raise ValidationError(
                f"card upgrade {card_id!r}: transitions must be contiguous from "
                f"T1, got {tiers} (§5.8.1)")
        # §5.8.2 — 곡선은 가파르다: 각 전이가 이전보다 확실히 비싸다.
        for previous, current in zip(rows, rows[1:]):
            if int(current["coin_cost"]) <= int(previous["coin_cost"]):
                raise ValidationError(
                    f"card upgrade {card_id!r} → T{current['target_tier']}: the "
                    "cost curve is steep, so each transition must cost more "
                    "than the last (§5.8.2)")


def _balance(db: Database, version_id: int):
    from app.content.balance import Balance

    return Balance(db, version_id)
