"""§15.1 — the stat pipeline and damage formula, and §2.10 elements.

Rounding is normative: full floating-point precision throughout, `floor`
applied **once**, to the final value, immediately before the `max(1, …)` clamp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from app.content.balance import Balance

# §2.10 — 6 elements plus 무속성. The affinity cycle is a single loop:
# 화 → 수 → 풍 → 지 → 광 → 암 → 화. Strong against the next, weak against
# the previous.
ELEMENT_CYCLE = ("화", "수", "풍", "지", "광", "암")
NEUTRAL_ELEMENT = "무속성"
ALL_ELEMENTS = ELEMENT_CYCLE + (NEUTRAL_ELEMENT,)


def element_affinity(balance: Balance, attacker: str, defender: str) -> float:
    """Multiplier for `attacker`'s element against `defender`'s.

    무속성 neither deals nor receives affinity modifiers.
    """
    if attacker == NEUTRAL_ELEMENT or defender == NEUTRAL_ELEMENT:
        return float(balance.get("element_affinity_neutral"))
    if attacker not in ELEMENT_CYCLE or defender not in ELEMENT_CYCLE:
        return float(balance.get("element_affinity_neutral"))

    size = len(ELEMENT_CYCLE)
    attacker_index = ELEMENT_CYCLE.index(attacker)
    defender_index = ELEMENT_CYCLE.index(defender)
    if (attacker_index + 1) % size == defender_index:
        return float(balance.get("element_affinity_advantage"))
    if (attacker_index - 1) % size == defender_index:
        return float(balance.get("element_affinity_disadvantage"))
    return float(balance.get("element_affinity_neutral"))


@dataclass
class BuildSnapshot:
    """The §16.2.3 frozen account build for one party slot.

    Every stat computation inside a run reads this, never live account rows —
    hub commands stay available during a run, so the live rows can move.
    """

    character_id: str
    star_rank: int
    research_stat_step: int
    job_role: str
    equipment: dict[str, dict] = field(default_factory=dict)   # {slot: {def_id, tier}}
    card_upgrades: dict[str, int] = field(default_factory=dict)
    equipment_flat: dict[str, int] = field(default_factory=dict)


def effective_stat(balance: Balance, stat: str, base_stat: float, *,
                   star_rank: int, research_step: int, equipment_flat: float) -> int:
    """§15.1 stat pipeline.

        effective = floor( base
                           × (1 + star_bonus_per_stat × (star_rank − 1))
                           × (1 + research_bonus)
                           + equipment_flat )

    속도 is never multiplied — its star bonus is 0 and research does not apply
    to it, so only the flat equipment term moves it.
    """
    star_bonus = float(balance.get("star_bonus_per_stat").get(stat, 0.0))
    star_multiplier = 1.0 + star_bonus * max(0, star_rank - 1)

    research_bonus = 0.0
    if stat in balance.get("research_stat_applies_to"):
        per_step = float(balance.get("research_stat_bonus_per_step"))
        max_steps = int(balance.get("research_stat_max_steps"))
        research_bonus = per_step * max(0, min(research_step, max_steps))

    value = base_stat * star_multiplier * (1.0 + research_bonus) + equipment_flat
    return int(math.floor(value))


def character_stats(balance: Balance, snapshot: BuildSnapshot) -> dict[str, int]:
    """Full stat block for one party member, from the run's build snapshot."""
    base = balance.get("base_stats_by_role")[snapshot.job_role]
    return {
        stat: effective_stat(
            balance, stat, base[stat],
            star_rank=snapshot.star_rank,
            research_step=snapshot.research_stat_step,
            equipment_flat=snapshot.equipment_flat.get(stat, 0),
        )
        for stat in ("hp", "atk", "def", "spd")
    }


# =====================================================================
# Damage
# =====================================================================
@dataclass
class DamageResult:
    final_damage: int      # after the floor and the minimum-damage clamp
    hp_loss: int           # after block
    block_consumed: int
    raw: float             # pre-floor, for telemetry and tests


def compute_damage(
    balance: Balance,
    *,
    attacker_atk: float,
    card_multiplier: float,
    target_def: float,
    attacker_element: str = NEUTRAL_ELEMENT,
    target_element: str = NEUTRAL_ELEMENT,
    attack_up_bonus: float = 0.0,
    defense_down_bonus: float = 0.0,
    target_block: int = 0,
    ignores_block: bool = False,
    ignores_defense: bool = False,
) -> DamageResult:
    """§15.1 damage formula.

        raw = (공격력 × multiplier − 방어력)
              × element_affinity × (1 + attack_up) × (1 + defense_down)
        final_damage = max(1, floor(raw))
        hp_loss      = max(0, final_damage − block)

    `ignores_block` covers 보호막 관통 and the flat-damage operators;
    화상/출혈 bypass the pipeline entirely and never reach this function.
    """
    defense = 0.0 if ignores_defense else float(target_def)
    raw = (float(attacker_atk) * float(card_multiplier)) - defense
    raw *= element_affinity(balance, attacker_element, target_element)
    raw *= 1.0 + attack_up_bonus
    raw *= 1.0 + defense_down_bonus

    floor_value = int(balance.get("minimum_damage_floor"))
    final_damage = max(floor_value, int(math.floor(raw)))

    if ignores_block:
        hp_loss = final_damage
        block_consumed = 0
    else:
        block_consumed = min(target_block, final_damage)
        hp_loss = max(0, final_damage - target_block)

    return DamageResult(final_damage=final_damage, hp_loss=hp_loss,
                        block_consumed=block_consumed, raw=raw)


def compute_flat_damage(amount: int, target_block: int, *,
                        ignores_block: bool) -> DamageResult:
    """Flat damage — 화상/출혈 and `deal_flat_damage`.

    §2.5.1 design note: flat damage ignoring block and 방어력 is a structural
    rule, not a knob. It answers high-defense enemies and gives 디버퍼형
    characters a reason to exist.
    """
    amount = max(0, int(amount))
    if ignores_block:
        return DamageResult(final_damage=amount, hp_loss=amount,
                            block_consumed=0, raw=float(amount))
    block_consumed = min(target_block, amount)
    return DamageResult(final_damage=amount, hp_loss=max(0, amount - target_block),
                        block_consumed=block_consumed, raw=float(amount))


def block_from_defense(balance: Balance, defense: float, mode: str,
                       value: float) -> int:
    """§2.5 / §15.1 block grants. `multiplier` scales 방어력; `flat` is literal."""
    if mode == "multiplier":
        return int(math.floor(float(defense) * float(value)))
    return int(math.floor(float(value)))


def auto_defend_block(balance: Balance, defense: float) -> int:
    """§2.3 / §2.11 P6 — no legal play resolves as block = 방어력 × 1.0."""
    return block_from_defense(
        balance, defense, "multiplier", balance.get("auto_defend_block_multiplier")
    )


def sum_percent(values: Iterable[float]) -> float:
    return float(sum(values))
