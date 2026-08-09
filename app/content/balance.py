"""§15 balancing constants.

> Every value here is loaded from config/DB and dashboard-editable (§10.1).
> **None may be hardcoded.**

`DEFAULT_CONSTANTS` exists solely to seed `balancing_constants` on a fresh
database. The engine never imports it — it reads through `Balance`, which
resolves against a pinned `content_version_id` so a run keeps the numbers it
started with (§10.6).
"""

from __future__ import annotations

import json
from typing import Any

from app.db.connection import Database

# =====================================================================
# §15.1 Combat core
# =====================================================================
DEFAULT_CONSTANTS: dict[str, Any] = {
    # -- deck & draw ---------------------------------------------------
    "draws_per_turn": 3,
    "base_deck_size": 18,

    # -- §2.3 resource pool, by party size -----------------------------
    "resource_pool_by_party_size": {"1": 3, "2": 4, "3": 5},
    "card_cost_min": 1,
    "card_cost_max": 3,

    # -- base character stats, 1★, per 직업·역할군 ---------------------
    "base_stats_by_role": {
        "공격형":     {"hp": 65, "atk": 12, "def": 5,  "spd": 105},
        "방어형":     {"hp": 95, "atk": 7,  "def": 10, "spd": 85},
        "서포터형":   {"hp": 70, "atk": 6,  "def": 6,  "spd": 100},
        "디버퍼형":   {"hp": 68, "atk": 8,  "def": 5,  "spd": 110},
        "딜서포트형": {"hp": 75, "atk": 10, "def": 6,  "spd": 95},
    },

    # -- stat pipeline -------------------------------------------------
    # 속도 is never multiplied — it moves only via 속도 감소 and flat equipment.
    "star_bonus_per_stat": {"hp": 0.12, "atk": 0.10, "def": 0.08, "spd": 0.0},
    "research_stat_bonus_per_step": 0.03,
    "research_stat_max_steps": 10,
    "research_stat_applies_to": ["hp", "atk", "def"],

    # -- damage formula ------------------------------------------------
    "element_affinity_advantage": 1.5,
    "element_affinity_disadvantage": 0.75,
    "element_affinity_neutral": 1.0,
    "minimum_damage_floor": 1,
    "auto_defend_block_multiplier": 1.0,
    "basic_defense_block_multiplier": 1.5,

    # -- enemy stat bands, main campaign -------------------------------
    "enemy_bands": {
        "일반":   {"hp": [45, 70],   "atk": [8, 12],  "def": [3, 6],  "spd": [80, 110]},
        "엘리트": {"hp": [110, 150], "atk": [14, 18], "def": [6, 10], "spd": [90, 115]},
        "보스":   {"hp": [450, 450], "atk": [20, 26], "def": [8, 12], "spd": [95, 105]},
    },

    # -- §10.5 summon bound check --------------------------------------
    "encounter_round_budget": 12,
    "max_enemies_per_encounter": 8,
    "max_party_slots": 3,

    # -- boss phase thresholds (per-boss override permitted) -----------
    "boss_phase_thresholds": [0.70, 0.30],

    # =================================================================
    # §15.2 Status effect magnitudes
    # =================================================================
    "status_magnitudes": {
        "화상":        {"magnitude": 3,     "duration": None, "stack_cap": None},
        "출혈":        {"magnitude": 4,     "duration": None, "stack_cap": None},
        "방어력_감소": {"magnitude": 0.08,  "duration": 3,    "stack_cap": 5},
        "공격력_증가": {"magnitude": 0.10,  "duration": 3,    "stack_cap": 5},
        "회복량_감소": {"magnitude": -0.15, "duration": 3,    "stack_cap": 4},
        "속도_감소":   {"magnitude": -8,    "duration": 3,    "stack_cap": 4},
        "기절":        {"magnitude": 0,     "duration": 1,    "stack_cap": None},
        "침묵":        {"magnitude": 0,     "duration": 2,    "stack_cap": None},
        "보호막_관통": {"magnitude": 0,     "duration": 2,    "stack_cap": None},
        "도발":        {"magnitude": 0,     "duration": 2,    "stack_cap": None},
    },

    # =================================================================
    # §15.3 Threat weight table — role_weight[enemy_role][ally_job_role]
    # §10.5 rejects a partially populated grid.
    # =================================================================
    "threat_weights": {
        "공격형":   {"공격형": 1.0, "방어형": 0.6, "서포터형": 2.5, "디버퍼형": 1.3, "딜서포트형": 1.3},
        "방어형":   {"공격형": 2.0, "방어형": 0.7, "서포터형": 1.0, "디버퍼형": 0.9, "딜서포트형": 1.4},
        "서포터형": {"공격형": 1.2, "방어형": 0.6, "서포터형": 1.1, "디버퍼형": 2.2, "딜서포트형": 1.0},
    },

    # =================================================================
    # §15.4 Gacha, growth, economy
    # =================================================================
    "gacha_base_rates": {"top": 0.015, "mid": 0.12, "base": 0.865},
    "gacha_character_split": 0.3,          # 3:7 character:card within every band
    "pity_soft_start": 70,                 # P_top is flat up to and including 70
    "pity_soft_increment": 0.05,           # +5.0%p per pull past the soft start
    "pity_hard": 90,
    "pity_mid_band_fixed": 0.12,           # 중간 holds while 기본 can absorb
    "gacha_cost_single": 160,
    "gacha_cost_ten": 1600,
    "gacha_ten_pull_size": 10,
    "first_pull_guarantee_window": 10,     # §5.10.1, counted by RESULTS
    "limited_5050_rate": 0.5,

    "carta_income": {
        "daily": 60,
        "weekly_attendance": 300,
        "run_clear_min": 80,
        "run_clear_max": 150,
        "achievement_min": 50,
        "achievement_max": 200,
    },

    # §15.4 C-07 run-clear coin
    "base_clear_coin": 1200,
    "per_depth_coin": 150,
    "first_clear_bonus": 5000,

    # duplicate conversion (§5.2)
    "duplicate_character_yield": {
        "1": {"fragments": 15, "wildcards": 1},
        "2": {"fragments": 30, "wildcards": 2},
        "3": {"fragments": 60, "wildcards": 3},
    },
    "duplicate_card_yield": {"1": 5, "2": 5, "3": 5, "4": 15, "5": 15, "6": 30},

    # 성급 상승 costs, keyed by the star rank being left (§15.4, 🟡 R-5)
    "star_up_costs": {
        "1": {"fragments": 10,  "wildcards": 1,  "coin": 3000},
        "2": {"fragments": 25,  "wildcards": 3,  "coin": 10000},
        "3": {"fragments": 50,  "wildcards": 8,  "coin": 30000},
        "4": {"fragments": 90,  "wildcards": 15, "coin": 70000},
        "5": {"fragments": 150, "wildcards": 30, "coin": 150000},
    },
    "star_cap_normal": 3,
    "star_cap_special": 6,

    # shop pricing
    "run_shop_card_price": [40, 110],
    "run_shop_effect_price": [25, 65],
    "hub_equipment_price": [3000, 15000],
    "hub_stone_price_coefficient": 400,     # 코인 400 × tier²
    "run_shop_stock": [4, 6],

    # 탐험 자금 income
    "run_currency_income": {
        "combat_clear": [25, 45],
        "elite_clear": [60, 90],
        "reward_node": [20, 40],
    },

    # drop rates
    "drop_rates": {
        "equipment_combat": 0.12,
        "equipment_reward": 0.25,
        "stone_combat": 0.35,
        "stone_combat_amount": [1, 2],
        "stone_reward": 0.60,
        "stone_reward_amount": [2, 4],
    },

    # =================================================================
    # §15.5 저주받은 카드
    # =================================================================
    "curse_removal_hp_pct": 0.15,
    "curse_removal_coin": 1200,

    # =================================================================
    # §15.6 boss transition effect parameters — timed effects, not statuses
    # =================================================================
    "transition_invulnerable_rounds": 1,
    "transition_awakening_rounds": 3,
    "transition_awakening_atk_pct": 30,
    "transition_awakening_spd_flat": 15,

    # =================================================================
    # §15.7 Map generation & event values
    # =================================================================
    "map_node_count": 15,
    "map_depth_structure": [1, 2, 3, 3, 3, 2, 1],
    "map_node_quota": {"전투": 7, "이벤트": 3, "보상": 2, "휴식": 2, "상점": 1},
    "map_generation_attempts": 16,
    "map_node_shuffle_attempts": 32,
    "rest_heal_pct": 0.30,
    "rest_heal_pct_tutorial": 0.50,
    "reward_cards_offered": 3,
    "reward_rarity_weights": {"low": 0.60, "mid": 0.33, "high": 0.07},
    "event_bands": {
        "small_coin": [200, 500],
        "large_card_fragments": [80, 200],
        "run_currency": [30, 80],
        "hp_cost_pct": [0.10, 0.20],
        "carta_drop_chance": 0.005,
        "carta_drop_amount": [150, 300],
        "curse_insert_chance": 0.35,
    },

    # =================================================================
    # §15.8 Equipment enhancement costs
    # B(1) is exempt — no T0 stone exists.
    # =================================================================
    "enhancement_costs": {
        "1": {"current": 2,  "previous": 0},
        "2": {"current": 3,  "previous": 2},
        "3": {"current": 4,  "previous": 4},
        "4": {"current": 6,  "previous": 7},
        "5": {"current": 8,  "previous": 11},
        "6": {"current": 10, "previous": 16},
    },
    "equipment_max_tier": 6,

    # =================================================================
    # §15.9 Tutorial world values
    # =================================================================
    "tutorial_enemy_band": {"hp": [30, 42], "atk": [9, 13], "def": [2, 4], "spd": [80, 100]},
    "tutorial_boss": {"hp": 100, "atk": [10, 13], "def": 4, "spd": 95},
    "tutorial_boss_thresholds": [0.50],
    "tutorial_enemies_per_node": [1, 2],
    "tutorial_reward_carta": 300,
    "tutorial_reward_party_slot": 2,
    "starter_deck_composition": {"평타": 6, "기본_방어": 5, "starter_skill": 7},

    # =================================================================
    # §15.10 Run inventory retention 🟡 R-3
    # Applied at run end when the run was NOT completed.
    # =================================================================
    "retention_bands": [
        {"max_depth": 2, "retain_rate": 0.00, "tier_down_chance": 0.00},
        {"max_depth": 4, "retain_rate": 0.40, "tier_down_chance": 0.60},
        {"max_depth": 6, "retain_rate": 0.65, "tier_down_chance": 0.35},
        {"max_depth": 7, "retain_rate": 0.80, "tier_down_chance": 0.20},
    ],
    "retention_boss_cleared": {"retain_rate": 1.00, "tier_down_chance": 0.00},

    # =================================================================
    # §16.3 scope, concurrency, expiry
    # =================================================================
    "inactivity_expiry_minutes": 30,
    "terminal_thread_retention_hours": 24,
    "starting_carta": 1600,
}


class Balance:
    """Reads §15 constants for one pinned content version.

    Values are cached per instance: a run's numbers cannot shift underneath it
    mid-battle even if the dashboard publishes during play.
    """

    def __init__(self, db: Database, content_version_id: int):
        self.db = db
        self.content_version_id = content_version_id
        self._cache: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        row = self.db.one(
            "SELECT value_json FROM balancing_constants "
            "WHERE content_version_id = ? AND key = ?",
            (self.content_version_id, key),
        )
        if row is None:
            if default is not None:
                return default
            raise KeyError(
                f"balancing constant {key!r} is not defined at content version "
                f"{self.content_version_id} — §15 values are never hardcoded, so a "
                "missing key is a content error"
            )
        value = json.loads(row["value_json"])
        self._cache[key] = value
        return value

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def int_(self, key: str) -> int:
        return int(self.get(key))

    def float_(self, key: str) -> float:
        return float(self.get(key))


def seed_constants(db: Database, content_version_id: int) -> None:
    """Write the §15 defaults into a content version. Idempotent."""
    with db.tx() as conn:
        for key, value in DEFAULT_CONSTANTS.items():
            conn.execute(
                "INSERT INTO balancing_constants (content_version_id, key, value_json) "
                "VALUES (?, ?, ?) ON CONFLICT(content_version_id, key) DO UPDATE SET "
                "value_json = excluded.value_json",
                (content_version_id, key, json.dumps(value, ensure_ascii=False)),
            )
