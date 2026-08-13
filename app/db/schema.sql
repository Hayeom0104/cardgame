-- Deckout schema — §18 of the design doc (v6.3).
-- Constraints here are NORMATIVE. Do not relax them.
-- SQLite, single file `deckout.db`, owned solely by this service.

-- =====================================================================
-- §18.9 Migration gate
-- =====================================================================
CREATE TABLE IF NOT EXISTS schema_version (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  version     INTEGER NOT NULL,
  applied_at  TEXT    NOT NULL
);

-- =====================================================================
-- §18.1 Account
-- 코인 is NEVER stored here. Read from Central on demand.
-- =====================================================================
CREATE TABLE IF NOT EXISTS accounts (
  user_id                   INTEGER PRIMARY KEY,   -- Discord snowflake
  created_at                TEXT    NOT NULL,
  updated_at                TEXT    NOT NULL,
  tutorial_completed_at     TEXT,                  -- NULL = not cleared
  party_slots               INTEGER NOT NULL DEFAULT 1,
  passive_slots             INTEGER NOT NULL DEFAULT 2,
  stat_research_step        INTEGER NOT NULL DEFAULT 0,   -- 0..10
  wildcards                 INTEGER NOT NULL DEFAULT 0,
  carta                     INTEGER NOT NULL DEFAULT 0,
  first_pull_results_count  INTEGER NOT NULL DEFAULT 0,   -- §5.10.1, 0..10
  first_pull_guarantee_used INTEGER NOT NULL DEFAULT 0    -- §5.10
);

-- §5.2 per-entity materials — never a single fungible balance column.
CREATE TABLE IF NOT EXISTS character_fragments (
  user_id      INTEGER NOT NULL,
  character_id TEXT    NOT NULL,
  amount       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, character_id)
);

CREATE TABLE IF NOT EXISTS card_fragments (
  user_id INTEGER NOT NULL,
  card_id TEXT    NOT NULL,
  amount  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, card_id)
);

-- §8.4 一 distinct item per tier.
CREATE TABLE IF NOT EXISTS enhancement_stones (
  user_id INTEGER NOT NULL,
  tier    INTEGER NOT NULL,
  amount  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, tier)
);

CREATE TABLE IF NOT EXISTS owned_characters (
  user_id      INTEGER NOT NULL,
  character_id TEXT    NOT NULL,
  star_rank    INTEGER NOT NULL DEFAULT 1,
  acquired_at  TEXT    NOT NULL,
  PRIMARY KEY (user_id, character_id)
);

-- §5.3 permanent unlock, §5.8 upgrade tier.
CREATE TABLE IF NOT EXISTS unlocked_cards (
  user_id      INTEGER NOT NULL,
  card_id      TEXT    NOT NULL,
  upgrade_tier INTEGER NOT NULL DEFAULT 0,
  unlocked_at  TEXT    NOT NULL,
  PRIMARY KEY (user_id, card_id)
);

CREATE TABLE IF NOT EXISTS owned_equipment (
  equipment_instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id               INTEGER NOT NULL,
  equipment_def_id      TEXT    NOT NULL,
  tier                  INTEGER NOT NULL DEFAULT 0,   -- T0..T6
  equipped_character_id TEXT,
  equipped_slot         TEXT,
  source_tx_id          TEXT                          -- §17.6 traceability
);
CREATE UNIQUE INDEX IF NOT EXISTS equip_one_per_slot
  ON owned_equipment (user_id, equipped_character_id, equipped_slot)
  WHERE equipped_character_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS research_unlocks (
  user_id     INTEGER NOT NULL,
  node_id     TEXT    NOT NULL,
  unlocked_at TEXT    NOT NULL,
  PRIMARY KEY (user_id, node_id)
);

-- §3.6 world unlock state is permanent, account-level.
CREATE TABLE IF NOT EXISTS world_unlocks (
  user_id          INTEGER NOT NULL,
  world_id         TEXT    NOT NULL,
  unlocked_at      TEXT    NOT NULL,
  first_cleared_at TEXT,
  PRIMARY KEY (user_id, world_id)
);

CREATE TABLE IF NOT EXISTS daily_claims (
  user_id    INTEGER NOT NULL,
  date_kst   TEXT    NOT NULL,
  claim_type TEXT    NOT NULL,
  PRIMARY KEY (user_id, date_kst, claim_type)
);

-- =====================================================================
-- §18.2 Gacha
-- §5.10.2: while first_pull_guarantee_used = 0 only 'standard_global'
-- may be pulled on; limited banners are hidden/disabled.
-- =====================================================================
CREATE TABLE IF NOT EXISTS gacha_pity (
  user_id           INTEGER NOT NULL,
  pity_scope_id     TEXT    NOT NULL,   -- 'standard_global' | 'limited_global'
  pull_count        INTEGER NOT NULL DEFAULT 0,
  guarantee_pending INTEGER NOT NULL DEFAULT 0,   -- 50/50 lost flag
  PRIMARY KEY (user_id, pity_scope_id)
);

CREATE TABLE IF NOT EXISTS gacha_transactions (
  gacha_id     TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL,
  banner_id    TEXT    NOT NULL,
  pull_kind    TEXT    NOT NULL,        -- 'single' | 'ten'
  carta_cost   INTEGER NOT NULL,
  commit_seed  TEXT    NOT NULL,        -- §5.9 server-only, never exposed
  results_json TEXT,
  status       TEXT    NOT NULL,
  created_at   TEXT    NOT NULL
);

-- =====================================================================
-- §18.3 Runs
-- =====================================================================
CREATE TABLE IF NOT EXISTS runs (
  run_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id                 INTEGER NOT NULL,
  world_id                TEXT    NOT NULL,
  state                   TEXT    NOT NULL,        -- §16.1
  is_tutorial             INTEGER NOT NULL DEFAULT 0,
  content_version_id      INTEGER NOT NULL,        -- §10.6 pinned, never changes
  map_seed                INTEGER NOT NULL,
  rng_seed                INTEGER NOT NULL,
  rng_counter             INTEGER NOT NULL DEFAULT 0,
  current_node_index      INTEGER,
  deepest_depth_reached   INTEGER NOT NULL DEFAULT 1,   -- §15.10 band selection
  run_currency            INTEGER NOT NULL DEFAULT 0,   -- 탐험 자금
  settlement_target_state TEXT,                    -- §16.2.1 written on ENTRY
  settlement_step         TEXT,                    -- inventory|rewards|achievements
                                                   -- |render|surface|done
  settlement_rng_op_key   TEXT,
  settlement_tx_id        TEXT,
  logical_session_id      TEXT    NOT NULL,
  surface_generation      INTEGER NOT NULL DEFAULT 1,
  thread_id               INTEGER,
  canonical_message_id    INTEGER,
  presentation_revision   INTEGER NOT NULL DEFAULT 0,
  created_at              TEXT    NOT NULL,
  updated_at              TEXT    NOT NULL,
  last_activity_at        TEXT    NOT NULL,
  ended_at                TEXT,
  end_reason              TEXT
);
-- §16.3 one active run per user, globally.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs (user_id)
  WHERE state NOT IN ('run_completed', 'run_defeated', 'run_abandoned',
                      'run_expired', 'admin_terminated');
CREATE INDEX IF NOT EXISTS runs_by_thread ON runs (thread_id);

-- §16.2.2 준비 화면 [1]-[4]. 이 단계들은 **순수 UI**라 run row를 만들지
-- 않으므로, 중간 선택은 여기 모인다. 계정당 한 줄이고 `one_active_run`과
-- 무관하다 — 도중에 그만두면 그냥 지워지고 아무 비용도 남지 않는다.
CREATE TABLE IF NOT EXISTS run_drafts (
  user_id      INTEGER PRIMARY KEY,
  world_id     TEXT,
  party_json   TEXT NOT NULL DEFAULT '[]',
  passive_json TEXT NOT NULL DEFAULT '[]',
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_nodes (
  run_id     INTEGER NOT NULL,
  node_index INTEGER NOT NULL,
  depth      INTEGER NOT NULL,
  node_type  TEXT    NOT NULL,
  state      TEXT    NOT NULL,
  PRIMARY KEY (run_id, node_index)
);

CREATE TABLE IF NOT EXISTS run_edges (
  run_id          INTEGER NOT NULL,
  from_node_index INTEGER NOT NULL,
  to_node_index   INTEGER NOT NULL,
  PRIMARY KEY (run_id, from_node_index, to_node_index)
);

CREATE TABLE IF NOT EXISTS run_characters (
  run_id       INTEGER NOT NULL,
  party_slot   INTEGER NOT NULL,
  character_id TEXT    NOT NULL,
  hp_current   INTEGER NOT NULL,
  hp_max       INTEGER NOT NULL,
  PRIMARY KEY (run_id, party_slot)
);

-- §16.2.3 the account build is frozen at run creation.
CREATE TABLE IF NOT EXISTS run_build_snapshot (
  run_id             INTEGER NOT NULL,
  party_slot         INTEGER NOT NULL,
  character_id       TEXT    NOT NULL,
  star_rank          INTEGER NOT NULL,
  research_stat_step INTEGER NOT NULL,
  equipment_json     TEXT    NOT NULL,   -- {slot: {def_id, tier}}
  card_upgrade_json  TEXT    NOT NULL,   -- {card_id: upgrade_tier}
  PRIMARY KEY (run_id, party_slot)
);

CREATE TABLE IF NOT EXISTS run_deck_cards (
  card_instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL,
  party_slot       INTEGER NOT NULL,
  card_id          TEXT    NOT NULL,
  is_cursed        INTEGER NOT NULL DEFAULT 0,
  pile             TEXT    NOT NULL,     -- draw | discard
  pile_position    INTEGER NOT NULL
);
-- §2.7.3 / M-09: a partially applied insertion shift fails loudly instead of
-- producing two cards at one index with SQL-order-dependent draw order.
CREATE UNIQUE INDEX IF NOT EXISTS deck_pile_position
  ON run_deck_cards (run_id, party_slot, pile, pile_position);

CREATE TABLE IF NOT EXISTS run_passives (
  run_id          INTEGER NOT NULL,
  passive_slot    INTEGER NOT NULL,
  passive_card_id TEXT    NOT NULL,
  PRIMARY KEY (run_id, passive_slot)
);

CREATE TABLE IF NOT EXISTS run_shop_items (
  run_id     INTEGER NOT NULL,
  node_index INTEGER NOT NULL,
  item_index INTEGER NOT NULL,
  item_ref   TEXT    NOT NULL,
  price      INTEGER NOT NULL,
  purchased  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, node_index, item_index)
);

-- §8.6 run inventory: permanent assets held until settlement.
CREATE TABLE IF NOT EXISTS run_inventory (
  entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER NOT NULL,
  kind              TEXT    NOT NULL,    -- 'equipment' | 'stone'
  equipment_def_id  TEXT,
  tier              INTEGER,
  stone_tier        INTEGER,
  amount            INTEGER NOT NULL DEFAULT 1,
  acquired_at_depth INTEGER NOT NULL,
  acquired_at       TEXT    NOT NULL
);

-- §16.5 pending choices / operator suspension.
CREATE TABLE IF NOT EXISTS pending_choices (
  choice_id                TEXT PRIMARY KEY,
  run_id                   INTEGER NOT NULL,
  node_index               INTEGER,
  choice_type              TEXT    NOT NULL,
  options_json             TEXT    NOT NULL,
  remaining_operators_json TEXT    NOT NULL,
  operator_cursor          INTEGER NOT NULL DEFAULT 0,
  selected_option          TEXT,
  status                   TEXT    NOT NULL,   -- open | resolved | cancelled
  rng_op_key               TEXT,
  created_at               TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_choice
  ON pending_choices (run_id) WHERE status = 'open';

-- §16.4 RNG operation journal.
CREATE TABLE IF NOT EXISTS rng_operations (
  run_id             INTEGER NOT NULL,
  op_key             TEXT    NOT NULL,
  rng_counter_before INTEGER NOT NULL,
  result_json        TEXT    NOT NULL,
  created_at         TEXT    NOT NULL,
  PRIMARY KEY (run_id, op_key)
);

-- §16.7 duplicate event protection.
CREATE TABLE IF NOT EXISTS processed_events (
  event_id   TEXT PRIMARY KEY,
  run_id     INTEGER,
  handled_at TEXT NOT NULL
);

-- =====================================================================
-- §18.4 Battles
-- =====================================================================
CREATE TABLE IF NOT EXISTS battles (
  battle_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id                    INTEGER NOT NULL,
  node_index                INTEGER NOT NULL,
  attempt_no                INTEGER NOT NULL DEFAULT 1,   -- §3.4.2 tutorial retry
  state                     TEXT    NOT NULL,
  round_no                  INTEGER NOT NULL DEFAULT 1,
  turn_cursor               INTEGER NOT NULL DEFAULT 0,
  turn_phase                TEXT,   -- await_card|await_target|resolving|await_nested_choice
  acting_unit_id            INTEGER,
  selected_card_instance_id INTEGER,
  pending_target_side       TEXT,
  selection_revision        INTEGER,
  party_resource_current    INTEGER NOT NULL,             -- §2.3 persisted
  source_encounter_id       TEXT    NOT NULL,             -- §15.1 downscaling audit
  applied_adjustment_json   TEXT,
  rng_seed                  INTEGER NOT NULL,
  rng_counter               INTEGER NOT NULL DEFAULT 0,
  created_at                TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS battle_node_attempt
  ON battles (run_id, node_index, attempt_no);

CREATE TABLE IF NOT EXISTS battle_units (
  battle_unit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  battle_id          INTEGER NOT NULL,
  side               TEXT    NOT NULL,      -- 'ally' | 'enemy'
  registration_order INTEGER NOT NULL,      -- §2.1 tie-breaker, monotonic
  visible_slot       INTEGER NOT NULL,      -- display only, never reclaimed
  unit_def_id        TEXT    NOT NULL,
  party_slot         INTEGER,               -- allies only: links to run_characters
  hp_current         INTEGER NOT NULL,
  hp_max             INTEGER NOT NULL,
  atk                INTEGER NOT NULL,
  def                INTEGER NOT NULL,
  spd                INTEGER NOT NULL,
  element            TEXT    NOT NULL,
  role               TEXT    NOT NULL,
  block              INTEGER NOT NULL DEFAULT 0,
  is_alive           INTEGER NOT NULL DEFAULT 1,
  boss_phase         INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS unit_reg_order
  ON battle_units (battle_id, side, registration_order);
CREATE UNIQUE INDEX IF NOT EXISTS unit_visible_slot
  ON battle_units (battle_id, side, visible_slot);

-- duration_remaining is NULL for model = stack_decay (§2.5.1, C-04).
CREATE TABLE IF NOT EXISTS battle_unit_statuses (
  battle_unit_id     INTEGER NOT NULL,
  status_id          TEXT    NOT NULL,
  stacks             INTEGER NOT NULL DEFAULT 1,
  duration_remaining INTEGER,
  PRIMARY KEY (battle_unit_id, status_id)
);

-- §2.5.3 round-scoped modifier INSTANCES, not statuses.
-- expires_after_round = applied_at_round + duration_rounds, so an effect
-- created in round N SURVIVES round N's boundary (§2.12).
CREATE TABLE IF NOT EXISTS battle_timed_effects (
  effect_instance_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  battle_id           INTEGER NOT NULL,
  battle_unit_id      INTEGER NOT NULL,
  effect_kind         TEXT    NOT NULL,   -- 'invulnerable' | 'stat_modifier'
  stat                TEXT,
  delta               REAL,
  is_percent          INTEGER,
  applied_at_round    INTEGER NOT NULL,
  expires_after_round INTEGER NOT NULL,
  source_ref          TEXT
);
CREATE INDEX IF NOT EXISTS timed_by_expiry
  ON battle_timed_effects (battle_id, expires_after_round);

-- §2.1.1 snapshot. `consumed` implements "a stunned unit still uses its entry".
CREATE TABLE IF NOT EXISTS battle_round_order (
  battle_id      INTEGER NOT NULL,
  round_no       INTEGER NOT NULL,
  order_index    INTEGER NOT NULL,
  battle_unit_id INTEGER NOT NULL,
  consumed       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (battle_id, round_no, order_index)
);

CREATE TABLE IF NOT EXISTS enemy_plans (
  battle_id                  INTEGER NOT NULL,
  enemy_unit_id              INTEGER NOT NULL,
  planned_action_id          TEXT    NOT NULL,
  planned_target_ids         TEXT    NOT NULL,
  planned_strategy_id        TEXT,             -- §2.8.6 needed to re-target
  planned_at_content_version INTEGER NOT NULL, -- §10.6 pin
  planned_at_revision        INTEGER,
  PRIMARY KEY (battle_id, enemy_unit_id)
);

-- §2.8.5. Set on EXECUTION only, never on planning.
CREATE TABLE IF NOT EXISTS enemy_cooldowns (
  battle_id            INTEGER NOT NULL,
  enemy_unit_id        INTEGER NOT NULL,
  action_id            TEXT    NOT NULL,
  available_from_round INTEGER NOT NULL,
  PRIMARY KEY (battle_id, enemy_unit_id, action_id)
);

CREATE TABLE IF NOT EXISTS battle_draw (
  battle_id        INTEGER NOT NULL,
  battle_unit_id   INTEGER NOT NULL,
  card_instance_id INTEGER NOT NULL,
  PRIMARY KEY (battle_id, battle_unit_id, card_instance_id)
);

-- =====================================================================
-- §18.5 Discord surface
-- =====================================================================
-- §1.3.3 written BEFORE sending; the callback carries neither
-- surface_generation nor presentation_revision.
CREATE TABLE IF NOT EXISTS delivery_intents (
  request_id            TEXT PRIMARY KEY,
  run_id                INTEGER,
  purpose               TEXT NOT NULL,
  surface_generation    INTEGER NOT NULL,
  presentation_revision INTEGER NOT NULL,
  created_at            TEXT NOT NULL
);

-- Populated ONLY from message_delivery_result (§1.3.3). Idempotent.
-- A callback whose surface_generation is older than runs.surface_generation
-- is RECORDED and IGNORED, never applied.
CREATE TABLE IF NOT EXISTS discord_bindings (
  request_id            TEXT PRIMARY KEY,
  run_id                INTEGER,
  purpose               TEXT,
  action                TEXT,
  success               INTEGER,
  partial               INTEGER,
  surface_generation    INTEGER,
  presentation_revision INTEGER,
  channel_id            INTEGER,
  message_id            INTEGER,
  thread_id             INTEGER,
  applied               INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_queue (
  queue_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL,
  target_revision     INTEGER NOT NULL,
  delivery_request_id TEXT    NOT NULL,
  payload_json        TEXT    NOT NULL,
  status              TEXT    NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT    NOT NULL
);
-- §16.6 guarantees already_applied on retry.
CREATE UNIQUE INDEX IF NOT EXISTS delivery_queue_request
  ON delivery_queue (delivery_request_id);

-- =====================================================================
-- §18.6 Transactions
-- =====================================================================
CREATE TABLE IF NOT EXISTS purchase_transactions (
  tx_id                  TEXT PRIMARY KEY,
  user_id                INTEGER NOT NULL,
  operation              TEXT    NOT NULL,
  direction              TEXT    NOT NULL,   -- deduct | grant | refund (§17.2)
  central_status         TEXT    NOT NULL,   -- pending|unknown|applied|rejected|partial
  local_status           TEXT    NOT NULL,   -- not_required | pending | applied
  expected_coin_delta    INTEGER NOT NULL,
  coin_applied_delta     INTEGER,
  coin_idempotency_key   TEXT    NOT NULL,
  refund_idempotency_key TEXT,
  local_payload          TEXT,
  status                 TEXT    NOT NULL,
  attempts               INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT    NOT NULL,
  updated_at             TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tx_coin_key
  ON purchase_transactions (coin_idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS tx_refund_key
  ON purchase_transactions (refund_idempotency_key)
  WHERE refund_idempotency_key IS NOT NULL;

-- §17.6 one receipt per transaction, ever.
CREATE TABLE IF NOT EXISTS fulfillment_receipts (
  tx_id        TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL,
  kind         TEXT    NOT NULL,
  payload_hash TEXT    NOT NULL,
  applied_at   TEXT    NOT NULL
);

-- =====================================================================
-- §18.7 Achievements
-- =====================================================================
CREATE TABLE IF NOT EXISTS achievement_progress (
  user_id           INTEGER NOT NULL,
  achievement_id    TEXT    NOT NULL,
  current_value     INTEGER NOT NULL DEFAULT 0,
  completed_at      TEXT,
  reward_claimed_at TEXT,
  PRIMARY KEY (user_id, achievement_id)
);

-- §20.2 C-06: covers non-run mutations that processed_events cannot.
CREATE TABLE IF NOT EXISTS achievement_progress_receipts (
  mutation_id    TEXT PRIMARY KEY,   -- "settle:<run_id>" | "starup:<tx_id>" | "event:<event_id>"
  user_id        INTEGER NOT NULL,
  achievement_id TEXT    NOT NULL,
  delta          INTEGER NOT NULL,
  applied_at     TEXT    NOT NULL
);

-- =====================================================================
-- §18.8 Content — every table keyed PRIMARY KEY (content_version_id, logical_id)
-- Published ids are IMMUTABLE and never reused; retirement is a flag.
-- =====================================================================
CREATE TABLE IF NOT EXISTS content_versions (
  version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  published_at TEXT,
  fingerprint  TEXT,
  is_current   INTEGER NOT NULL DEFAULT 0,
  is_draft     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cards (
  content_version_id INTEGER NOT NULL,
  card_id            TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  element            TEXT    NOT NULL,   -- §2.10, one of 7
  cost               INTEGER NOT NULL,
  category           TEXT    NOT NULL,   -- 공격 | 방어 | 버프디버프 | 회복
  target_side        TEXT    NOT NULL,   -- enemy | ally | self | all (§2.8.2)
  rarity_tier        INTEGER NOT NULL,   -- §5.6, 1..6
  effects_json       TEXT    NOT NULL,   -- ordered [{operator, params}]
  art_asset          TEXT,
  is_retired         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, card_id)
);

CREATE TABLE IF NOT EXISTS characters (
  content_version_id INTEGER NOT NULL,
  character_id       TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  element            TEXT    NOT NULL,
  job_role           TEXT    NOT NULL,   -- §4.5.1
  base_rarity        INTEGER NOT NULL DEFAULT 1,
  special_cap        INTEGER NOT NULL DEFAULT 0,   -- §4.4, 6★ ceiling
  in_gacha_pool      INTEGER NOT NULL DEFAULT 1,
  art_asset          TEXT,
  is_retired         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, character_id)
);

CREATE TABLE IF NOT EXISTS enemies (
  content_version_id INTEGER NOT NULL,
  enemy_id           TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  tier               TEXT    NOT NULL,   -- 일반 | 엘리트 | 보스
  element            TEXT    NOT NULL,
  role               TEXT    NOT NULL,   -- §2.8.3 enemy role
  hp                 INTEGER NOT NULL,
  atk                INTEGER NOT NULL,
  def                INTEGER NOT NULL,
  spd                INTEGER NOT NULL,
  strategy_override  TEXT,
  action_rules_json  TEXT    NOT NULL,   -- §2.8.5 ordered rule list
  art_asset          TEXT,
  is_retired         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, enemy_id)
);

CREATE TABLE IF NOT EXISTS enemy_actions (
  content_version_id INTEGER NOT NULL,
  action_id          TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  category           TEXT    NOT NULL,   -- 공격 | 방어 | 버프디버프 | 회복
  target_side        TEXT    NOT NULL,
  is_basic_attack    INTEGER NOT NULL DEFAULT 0,   -- §2.8.5 step 4
  effects_json       TEXT    NOT NULL,
  PRIMARY KEY (content_version_id, action_id)
);

CREATE TABLE IF NOT EXISTS encounters (
  content_version_id INTEGER NOT NULL,
  encounter_id       TEXT    NOT NULL,
  world_id           TEXT    NOT NULL,
  kind               TEXT    NOT NULL,   -- normal | elite | boss
  units_json         TEXT    NOT NULL,   -- [{enemy_id, slot, tier_rank}]
  PRIMARY KEY (content_version_id, encounter_id)
);

CREATE TABLE IF NOT EXISTS worlds (
  content_version_id INTEGER NOT NULL,
  world_id           TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  sequence_index     INTEGER NOT NULL,
  is_tutorial        INTEGER NOT NULL DEFAULT 0,
  drop_tier_min      INTEGER NOT NULL DEFAULT 0,   -- §8.6.1 band
  drop_tier_max      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, world_id)
);

CREATE TABLE IF NOT EXISTS equipment_defs (
  content_version_id INTEGER NOT NULL,
  equipment_def_id   TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  slot               TEXT    NOT NULL,   -- 무기 | 방어구 | 악세서리
  set_name           TEXT,
  hp_flat            INTEGER NOT NULL DEFAULT 0,
  atk_flat           INTEGER NOT NULL DEFAULT 0,
  def_flat           INTEGER NOT NULL DEFAULT 0,
  spd_flat           INTEGER NOT NULL DEFAULT 0,
  price_coin         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, equipment_def_id)
);

CREATE TABLE IF NOT EXISTS equipment_sets (
  content_version_id INTEGER NOT NULL,
  set_name           TEXT    NOT NULL,
  bonus_json         TEXT    NOT NULL,
  PRIMARY KEY (content_version_id, set_name)
);

CREATE TABLE IF NOT EXISTS statuses (
  content_version_id          INTEGER NOT NULL,
  status_id                   TEXT    NOT NULL,
  name                        TEXT    NOT NULL,
  kind                        TEXT    NOT NULL,   -- buff | debuff
  model                       TEXT    NOT NULL,   -- countdown|stack_duration|stack_decay
  clock                       TEXT    NOT NULL,   -- turn_start_trigger|owner_turn_countdown
  stack_cap                   INTEGER,            -- NULL for countdown
  base_duration               INTEGER,            -- NULL for stack_decay
  magnitude                   REAL    NOT NULL DEFAULT 0,
  cleansable                  INTEGER NOT NULL DEFAULT 1,
  persists_through_boss_phase INTEGER NOT NULL DEFAULT 0,
  icon_asset                  TEXT,
  -- §2.5.1a [v6.4] gates which content pools may reference this status.
  -- player_only | enemy_only | universal. Declared once at authoring time,
  -- never re-decided per use. The base 10 are all `universal`.
  scope                       TEXT NOT NULL DEFAULT 'universal',
  PRIMARY KEY (content_version_id, status_id)
);

CREATE TABLE IF NOT EXISTS targeting_strategies (
  content_version_id   INTEGER NOT NULL,
  strategy_id          TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  selector_operator    TEXT    NOT NULL,   -- §10.4.4 closed set
  valid_target_filter  TEXT,
  scoring_expression   TEXT,               -- NULL for non-scoring selectors
  tie_breaker          TEXT    NOT NULL DEFAULT 'registration_order',
  fallback_strategy_id TEXT,
  params_json          TEXT,
  PRIMARY KEY (content_version_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS enemy_roles (
  content_version_id   INTEGER NOT NULL,
  enemy_role_id        TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  default_strategy_id  TEXT    NOT NULL,
  fallback_strategy_id TEXT,
  PRIMARY KEY (content_version_id, enemy_role_id)
);

CREATE TABLE IF NOT EXISTS threat_weights (
  content_version_id INTEGER NOT NULL,
  enemy_role_id      TEXT    NOT NULL,
  ally_role_id       TEXT    NOT NULL,
  weight             REAL    NOT NULL,
  PRIMARY KEY (content_version_id, enemy_role_id, ally_role_id)
);

CREATE TABLE IF NOT EXISTS boss_phases (
  content_version_id  INTEGER NOT NULL,
  boss_phase_id       TEXT    NOT NULL,
  enemy_id            TEXT    NOT NULL,
  phase_index         INTEGER NOT NULL,
  hp_threshold_pct    REAL    NOT NULL,   -- entered when hp% <= this
  effect_ids_json     TEXT    NOT NULL,   -- ordered transition effect ids
  PRIMARY KEY (content_version_id, boss_phase_id)
);

CREATE TABLE IF NOT EXISTS transition_effects (
  content_version_id   INTEGER NOT NULL,
  transition_effect_id TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  effects_json         TEXT    NOT NULL,   -- §10.4 operator list
  PRIMARY KEY (content_version_id, transition_effect_id)
);

CREATE TABLE IF NOT EXISTS cursed_cards (
  content_version_id INTEGER NOT NULL,
  cursed_card_id     TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  art_asset          TEXT,
  penalty_json       TEXT    NOT NULL,   -- §10.4 operator list
  flavor_text        TEXT,
  removable          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (content_version_id, cursed_card_id)
);

CREATE TABLE IF NOT EXISTS events (
  content_version_id INTEGER NOT NULL,
  event_id           TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  interaction_kind   TEXT    NOT NULL,   -- choice | instant
  branches_json      TEXT    NOT NULL,   -- [{label, effects:[...]}]
  combat_link        TEXT    NOT NULL,   -- none | fixed | conditional
  PRIMARY KEY (content_version_id, event_id)
);

CREATE TABLE IF NOT EXISTS reward_tables (
  content_version_id INTEGER NOT NULL,
  reward_table_id    TEXT    NOT NULL,
  entries_json       TEXT    NOT NULL,
  PRIMARY KEY (content_version_id, reward_table_id)
);

CREATE TABLE IF NOT EXISTS banners (
  content_version_id INTEGER NOT NULL,
  banner_id          TEXT    NOT NULL,
  banner_type        TEXT    NOT NULL,   -- standard | limited
  pickup_type        TEXT,               -- character | card | null
  pickup_target_id   TEXT,
  start_at           TEXT,
  end_at             TEXT,
  pity_scope_id      TEXT    NOT NULL,   -- §5.4.2
  PRIMARY KEY (content_version_id, banner_id)
);

CREATE TABLE IF NOT EXISTS achievements (
  content_version_id INTEGER NOT NULL,
  achievement_id     TEXT    NOT NULL,
  name               TEXT    NOT NULL,
  description        TEXT,
  icon_asset         TEXT,
  counter_key        TEXT    NOT NULL,
  target_value       INTEGER NOT NULL,
  carta_reward       INTEGER NOT NULL DEFAULT 0,
  is_hidden          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_version_id, achievement_id)
);

CREATE TABLE IF NOT EXISTS research_nodes (
  content_version_id   INTEGER NOT NULL,
  node_id              TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  coin_cost            INTEGER NOT NULL,
  wildcard_cost        INTEGER NOT NULL,
  required_achievement TEXT,
  effect_json          TEXT    NOT NULL,
  max_steps            INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (content_version_id, node_id)
);

-- §5.8 [v6.4, P-1] card upgrade definitions, one row per transition.
-- Five tiers per card: upgrade_tier ∈ {0..5}, where 0 is the un-upgraded pulled
-- state, so five transitions 0→1 … 4→5. Per-card costs and per-tier effect
-- selection are content authoring (§5.8.5); the rules they must follow are
-- enforced by §10.5.
CREATE TABLE IF NOT EXISTS card_upgrades (
  content_version_id INTEGER NOT NULL,
  card_id            TEXT    NOT NULL,
  target_tier        INTEGER NOT NULL,   -- 1..5, the tier being reached
  fragment_cost      INTEGER NOT NULL,
  wildcard_cost      INTEGER NOT NULL DEFAULT 0,   -- 0 below 2→3 (§5.8.2)
  coin_cost          INTEGER NOT NULL,
  effects_json       TEXT    NOT NULL,   -- ordered [{operator, params}] overlay
  PRIMARY KEY (content_version_id, card_id, target_tier)
);

-- §15 — every balancing value lives here. NONE may be hardcoded.
CREATE TABLE IF NOT EXISTS balancing_constants (
  content_version_id INTEGER NOT NULL,
  key                TEXT    NOT NULL,
  value_json         TEXT    NOT NULL,
  PRIMARY KEY (content_version_id, key)
);

-- 설정마다 "무엇을 바꾸는 값인지"에 대한 설명. `config/*.toml` 의 주석에서
-- 그대로 읽어오므로 파일과 대시보드의 설명이 갈라지지 않는다.
-- 값이 아니라 키를 설명하는 것이므로 콘텐츠 버전에 묶이지 않는다.
CREATE TABLE IF NOT EXISTS balancing_metadata (
  key         TEXT PRIMARY KEY,
  source_file TEXT NOT NULL,   -- 이 설정이 정의된 config 파일 이름
  description TEXT NOT NULL
);
