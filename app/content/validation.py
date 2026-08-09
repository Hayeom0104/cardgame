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
    _validate_research(db, version_id)


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
    for row in db.query("SELECT * FROM cursed_cards WHERE content_version_id = ?",
                        (version_id,)):
        try:
            ops.validate_effect_list(_effects(row["penalty_json"]),
                                     ops.CTX_CURSED_CARD)
        except ValidationError as error:
            raise ValidationError(
                f"cursed card {row['cursed_card_id']!r}: {error}") from error


def _validate_enemy_actions(db: Database, version_id: int) -> None:
    for row in db.query("SELECT * FROM enemy_actions WHERE content_version_id = ?",
                        (version_id,)):
        label = f"enemy action {row['action_id']!r}"
        if row["target_side"] not in VALID_TARGET_SIDES:
            raise ValidationError(f"{label}: target_side {row['target_side']!r} is invalid")
        try:
            ops.validate_effect_list(_effects(row["effects_json"]),
                                     ops.CTX_ENEMY_ACTION)
        except ValidationError as error:
            raise ValidationError(f"{label}: {error}") from error


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


def _balance(db: Database, version_id: int):
    from app.content.balance import Balance

    return Balance(db, version_id)
