"""§10.4 — the content effect engine's operator whitelist.

Inserting a database row does not make new logic executable. The dashboard
composes new *combinations and parameter values* from the operators registered
here; a genuinely new mechanic requires a code deployment (§10.4.6).

Two orthogonal tags govern where an operator may appear:

  Category (§10.4.1)     — how it executes: inline, suspending, or terminal.
                           Computed from (operator, params), not the name alone.
  Host context (§10.4.1a) — BATTLE_SAFE vs PROGRESSION, which is what stops a
                           dashboard editor from authoring a battle card that
                           grants permanent fragments every time it is played.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# -- categories (§10.4.1) ------------------------------------------------
PURE_SYNCHRONOUS = "PURE_SYNCHRONOUS"
PENDING_CHOICE = "PENDING_CHOICE"
CENTRAL_TRANSACTION = "CENTRAL_TRANSACTION"
TERMINAL_STATE_TRANSITION = "TERMINAL_STATE_TRANSITION"

# -- host-context tags (§10.4.1a) ---------------------------------------
BATTLE_SAFE = "BATTLE_SAFE"
PROGRESSION = "PROGRESSION"

# -- host contexts -------------------------------------------------------
CTX_BATTLE_CARD = "battle_card"
CTX_PASSIVE = "passive"
CTX_ENEMY_ACTION = "enemy_action"
CTX_CURSED_CARD = "cursed_card"
CTX_TRANSITION_EFFECT = "transition_effect"
CTX_EVENT = "event"
CTX_REWARD = "reward"
CTX_SHOP = "shop"
CTX_SETTLEMENT = "settlement"
#: §5.8 카드 업그레이드 오버레이. 전투 카드와 같은 제약을 받되(§10.4.1a), 카드
#: 정의 자체가 아니라 그 위에 덧씌워지는 목록이다.
CTX_CARD_UPGRADE = "card_upgrade"
#: §2.13 반응형 능력의 effect_operators. 카드/패시브와 같은 제약(PURE ∩
#: BATTLE_SAFE)을 받는다 — 피격 즉시 전투 턴 안에서 실행되기 때문이다.
CTX_REACTIVE_ABILITY = "reactive_ability"

#: Contexts that resolve inside a battle turn's effect list. §10.4.1a restricts
#: these to PURE_SYNCHRONOUS ∩ BATTLE_SAFE.
IN_BATTLE_CONTEXTS = frozenset(
    {CTX_BATTLE_CARD, CTX_PASSIVE, CTX_ENEMY_ACTION, CTX_CURSED_CARD,
     CTX_TRANSITION_EFFECT, CTX_CARD_UPGRADE, CTX_REACTIVE_ABILITY}
)

#: Contexts where PROGRESSION operators are legal.
PROGRESSION_CONTEXTS = frozenset({CTX_EVENT, CTX_REWARD, CTX_SHOP, CTX_SETTLEMENT})


class ValidationError(ValueError):
    """Raised by §10.5 validation. Blocks the save, or fails startup closed."""


@dataclass(frozen=True)
class ParamSpec:
    name: str
    types: tuple[type, ...]
    required: bool = True
    choices: tuple[Any, ...] | None = None
    default: Any = None


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    host_tag: str
    params: tuple[ParamSpec, ...]
    #: Returns the category for a concrete parameter set. §10.4.1: category is
    #: computed from (operator, params) — `discard_cards(selector=random)` is
    #: pure while `discard_cards(selector=choose)` suspends.
    categorize: Callable[[dict[str, Any]], str] = field(
        default=lambda params: PURE_SYNCHRONOUS
    )

    def validate_params(self, params: dict[str, Any]) -> None:
        known = {p.name for p in self.params}
        for key in params:
            if key not in known:
                raise ValidationError(f"{self.name}: unknown parameter {key!r}")
        for spec in self.params:
            if spec.name not in params:
                if spec.required:
                    raise ValidationError(f"{self.name}: missing parameter {spec.name!r}")
                continue
            value = params[spec.name]
            if value is None and not spec.required:
                continue
            if not isinstance(value, spec.types) or isinstance(value, bool) and bool not in spec.types:
                expected = "/".join(t.__name__ for t in spec.types)
                raise ValidationError(
                    f"{self.name}.{spec.name}: expected {expected}, got {type(value).__name__}"
                )
            if spec.choices is not None and value not in spec.choices:
                raise ValidationError(
                    f"{self.name}.{spec.name}: {value!r} not in {list(spec.choices)}"
                )


def _pure(_params: dict[str, Any]) -> str:
    return PURE_SYNCHRONOUS


def _selector_choose(params: dict[str, Any]) -> str:
    """`selector=choose` suspends the list on a player decision."""
    return PENDING_CHOICE if params.get("selector") == "choose" else PURE_SYNCHRONOUS


def _recipient_chooser(params: dict[str, Any]) -> str:
    return PENDING_CHOICE if params.get("recipient") == "chooser" else PURE_SYNCHRONOUS


def _currency_central(params: dict[str, Any]) -> str:
    """§10.4.3: CENTRAL when currency = 코인 — everything else is local."""
    return CENTRAL_TRANSACTION if params.get("currency") == "coin" else PURE_SYNCHRONOUS


def _always(category: str) -> Callable[[dict[str, Any]], str]:
    return lambda _params: category


#: §10.4.3 operator table. Registration = implementation + parameter schema +
#: category rule (§10.4.6 steps 1-2).
OPERATORS: dict[str, OperatorSpec] = {}


def _register(spec: OperatorSpec) -> None:
    OPERATORS[spec.name] = spec


#: §10.4.3 [NEW, this session] — optional on both damage operators. Default
#: crit_chance = 0. On success the damage computation uses crit_multiplier in
#: place of (not stacked with) multiplier/amount's normal scaling.
#: Card-authored per-card, not a character stat — no §15.1 stat pipeline entry.
_CRIT_PARAMS = (
    ParamSpec("crit_chance", (int, float), required=False, default=0),
    ParamSpec("crit_multiplier", (int, float), required=False),
)

_register(OperatorSpec("deal_damage", BATTLE_SAFE, (
    ParamSpec("multiplier", (int, float)),
    ParamSpec("ignores_block", (bool,), required=False, default=False),
    ParamSpec("ignores_defense", (bool,), required=False, default=False),
    *_CRIT_PARAMS,
), _pure))

_register(OperatorSpec("deal_flat_damage", BATTLE_SAFE, (
    ParamSpec("amount", (int,)),
    ParamSpec("ignores_block", (bool,), required=False, default=False),
    ParamSpec("ignores_defense", (bool,), required=False, default=False),
    *_CRIT_PARAMS,
), _pure))

_register(OperatorSpec("grant_block", BATTLE_SAFE, (
    ParamSpec("mode", (str,), choices=("multiplier", "flat")),
    ParamSpec("value", (int, float)),
), _pure))

_register(OperatorSpec("heal", BATTLE_SAFE, (
    ParamSpec("mode", (str,), choices=("flat", "percent_max_hp")),
    ParamSpec("value", (int, float)),
), _pure))

# §5.8.3 [v6.4] — 카드 업그레이드가 추가하는 상태는 카드 자신의 target_side와
# 독립적인 대상을 가질 수 있다: "target: self | single enemy | all enemies |
# all allies | one designated ally". 그 자유도가 없으면 공격 카드에 붙인 자기
# 버프가 적에게 걸린다.
APPLY_STATUS_TARGETS = ("self", "single_enemy", "all_enemies", "all_allies",
                        "designated_ally")

_register(OperatorSpec("apply_status", BATTLE_SAFE, (
    ParamSpec("status_id", (str,)),
    ParamSpec("stacks", (int,), required=False, default=1),
    ParamSpec("duration_override", (int,), required=False),
    ParamSpec("target", (str,), required=False, choices=APPLY_STATUS_TARGETS),
    ParamSpec("party_slot", (int,), required=False),
), _pure))

_register(OperatorSpec("remove_status", BATTLE_SAFE, (
    ParamSpec("status_id", (str,), required=False),
    ParamSpec("category", (str,), required=False, choices=("buff", "debuff")),
    ParamSpec("count", (int,), required=False, default=1),
), _pure))

_register(OperatorSpec("modify_resource", BATTLE_SAFE, (
    ParamSpec("delta", (int,)),
), _pure))

_register(OperatorSpec("draw_cards", BATTLE_SAFE, (
    ParamSpec("count", (int,)),
), _pure))

_register(OperatorSpec("discard_cards", BATTLE_SAFE, (
    ParamSpec("count", (int,)),
    ParamSpec("selector", (str,), choices=("random", "choose")),
), _selector_choose))

_register(OperatorSpec("insert_cursed_card", BATTLE_SAFE, (
    ParamSpec("cursed_card_id", (str,)),
    ParamSpec("position", (str,), required=False, default="random", choices=("random",)),
), _pure))

# §2.7.4: auto-resolves if exactly one exists; no-op (and no charge) if none.
_register(OperatorSpec("remove_cursed_card", BATTLE_SAFE, (
    ParamSpec("count", (int,), required=False, default=1),
), _always(PENDING_CHOICE)))

_register(OperatorSpec("modify_stat", BATTLE_SAFE, (
    ParamSpec("stat", (str,), choices=("hp", "atk", "def", "spd")),
    ParamSpec("delta", (int, float)),
    ParamSpec("is_percent", (bool,), required=False, default=False),
    ParamSpec("duration_rounds", (int,)),
), _pure))

_register(OperatorSpec("set_invulnerable", BATTLE_SAFE, (
    ParamSpec("duration_rounds", (int,)),
), _pure))

_register(OperatorSpec("add_card_to_run_deck", BATTLE_SAFE, (
    ParamSpec("card_id", (str,)),
    ParamSpec("recipient", (str,), choices=("acting", "chooser")),
), _recipient_chooser))

_register(OperatorSpec("remove_card_from_run_deck", BATTLE_SAFE, (
    ParamSpec("selector", (str,), choices=("random", "chooser")),
), lambda p: PENDING_CHOICE if p.get("selector") == "chooser" else PURE_SYNCHRONOUS))

_register(OperatorSpec("modify_hp", BATTLE_SAFE, (
    ParamSpec("mode", (str,), choices=("flat", "percent_max_hp")),
    ParamSpec("delta", (int, float)),
), _pure))

# §5.8 [v6.4] — 업그레이드 효과는 §10.4 연산자로 표현 가능해야 하고, 그 중
# "cost change"에 해당하는 연산자가 없었다. §10.4.6 절차대로 등록한다: 구현 +
# 파라미터 스키마 + 카테고리 규칙. 오버레이가 카드의 `cost` 필드에 적용하므로
# 전투 중에 실행되는 일은 없다.
_register(OperatorSpec("modify_cost", BATTLE_SAFE, (
    ParamSpec("delta", (int,)),
), _pure))

_register(OperatorSpec("summon_enemy", BATTLE_SAFE, (
    ParamSpec("enemy_id", (str,)),
    ParamSpec("count", (int,)),
), _pure))

# -- PROGRESSION (§10.4.1a) ---------------------------------------------
_register(OperatorSpec("grant_currency", PROGRESSION, (
    ParamSpec("currency", (str,),
              choices=("coin", "carta", "wildcard", "run_currency")),
    ParamSpec("amount", (int,)),
), _currency_central))

_register(OperatorSpec("grant_character_fragments", PROGRESSION, (
    ParamSpec("character_id", (str,)),
    ParamSpec("amount", (int,)),
), _pure))

_register(OperatorSpec("grant_card_fragments", PROGRESSION, (
    ParamSpec("card_id", (str,)),
    ParamSpec("amount", (int,)),
), _pure))

_register(OperatorSpec("grant_equipment", PROGRESSION, (
    ParamSpec("equipment_id", (str,), required=False),
    ParamSpec("rarity_band", (str,), required=False),
), _pure))

_register(OperatorSpec("grant_enhancement_stone", PROGRESSION, (
    ParamSpec("tier", (int,)),
    ParamSpec("amount", (int,)),
), _pure))

_register(OperatorSpec("offer_reward", PROGRESSION, (
    ParamSpec("reward_table_id", (str,)),
), _always(PENDING_CHOICE)))

_register(OperatorSpec("start_combat", PROGRESSION, (
    ParamSpec("encounter_id", (str,)),
), _always(TERMINAL_STATE_TRANSITION)))

_register(OperatorSpec("grant_achievement_progress", PROGRESSION, (
    ParamSpec("achievement_id", (str,)),
    ParamSpec("delta", (int,)),
), _pure))


# -- §10.4.4 selector operators -----------------------------------------
SELECTOR_OPERATORS = frozenset({
    "lowest_hp_absolute", "lowest_hp_percent", "highest_hp_absolute",
    "random_uniform", "highest_score", "has_status",
    "lacks_status_category", "fixed_slot",
})

SCORING_EXPRESSIONS = frozenset({"threat_score", "current_attack", "missing_hp"})

# -- §10.4.5 condition operators ----------------------------------------
CONDITION_OPERATORS = frozenset({
    "always", "self_hp_below", "self_hp_above", "any_ally_hp_below",
    "own_side_count_below", "opposing_side_count_below",
    "self_has_status", "target_has_status", "round_number_gte",
    "phase_is", "owner_turn_index_mod",
    "random_chance",   # [NEW, this session] — journaled via §16.4, not a bare RNG call
})


def categorize(operator: str, params: dict[str, Any]) -> str:
    """Return the §10.4.1 category for a concrete (operator, params) pair."""
    spec = OPERATORS.get(operator)
    if spec is None:
        raise ValidationError(f"unknown operator {operator!r}")
    return spec.categorize(params)


def host_tag(operator: str) -> str:
    spec = OPERATORS.get(operator)
    if spec is None:
        raise ValidationError(f"unknown operator {operator!r}")
    return spec.host_tag


def validate_effect_list(effects: list[dict[str, Any]], context: str) -> None:
    """§10.4.1 / §10.4.1a — validate one ordered operator list for its host.

    Raises ValidationError on the first violation. Content is never partially
    accepted (§10.5).
    """
    if not isinstance(effects, list):
        raise ValidationError("effect list must be a list")

    for index, entry in enumerate(effects):
        if not isinstance(entry, dict) or "operator" not in entry:
            raise ValidationError(f"effect[{index}] must be {{operator, params}}")
        operator = entry["operator"]
        params = entry.get("params") or {}
        spec = OPERATORS.get(operator)
        if spec is None:
            raise ValidationError(f"effect[{index}]: unknown operator {operator!r}")
        spec.validate_params(params)

        # §10.4.3 — crit_chance/crit_multiplier bounds. Only deal_damage and
        # deal_flat_damage declare these params, so this only ever fires for
        # them (an unknown key on any other operator already failed above).
        crit_chance = params.get("crit_chance")
        if crit_chance is not None and not 0.0 <= float(crit_chance) <= 1.0:
            raise ValidationError(f"effect[{index}]: crit_chance must be 0..1")
        crit_multiplier = params.get("crit_multiplier")
        if crit_multiplier is not None and float(crit_multiplier) <= 0:
            raise ValidationError(f"effect[{index}]: crit_multiplier must be > 0")

        category = spec.categorize(params)

        # TERMINAL_STATE_TRANSITION must be last; anything after it is
        # unreachable (§10.4.1a).
        if category == TERMINAL_STATE_TRANSITION and index != len(effects) - 1:
            raise ValidationError(
                f"effect[{index}]: {operator!r} is TERMINAL_STATE_TRANSITION and "
                "must be the last operator in its list"
            )

        if context in IN_BATTLE_CONTEXTS:
            if category != PURE_SYNCHRONOUS:
                raise ValidationError(
                    f"effect[{index}]: {operator!r} resolves as {category} which is "
                    f"illegal in host context {context!r} — battle-turn effect lists "
                    "accept PURE_SYNCHRONOUS only"
                )
            if spec.host_tag != BATTLE_SAFE:
                raise ValidationError(
                    f"effect[{index}]: {operator!r} is PROGRESSION-tagged and may not "
                    f"appear in host context {context!r} (§10.4.1a)"
                )
        elif spec.host_tag == PROGRESSION and context not in PROGRESSION_CONTEXTS:
            raise ValidationError(
                f"effect[{index}]: PROGRESSION operator {operator!r} is legal only in "
                f"event, reward, shop and settlement contexts, not {context!r}"
            )
