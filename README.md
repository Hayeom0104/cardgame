# Deckout

An implementation of **Deckout — Design Document v6.3**: a roguelike card-battler
that runs as its own FastAPI service and proxies every Discord interaction
through the ARI Central Bot.

The service holds **no** Discord gateway connection and **no** bot token
(§1.1). Base command `!덱아웃`, dedicated channel `Deckout`, component
namespace `dko:`.

```bash
pip install -e ".[dev]"
python -m app.cli.bootstrap --db deckout.db      # migrate + seed + publish content
python -m app.cli.check_content --db deckout.db  # §10.5 validation pass
uvicorn app.api.server:app --port 8080
pytest                                           # 274 tests
```

Registration (§1.3.0) — `route_threads: true` is **mandatory** and defaults to
false. Every run lives in a private thread, so without it no in-run component
interaction is ever delivered and the game is non-functional:

```yaml
minigames:
  deckout:
    channel: "<parent-channel-id>"      # guide §6 key name — NOT `channel_id`
    service_url: "http://localhost:8080"
    route_threads: true
```

API-key registration is run on the **Central Bot** side, not here (§1.3.9):

```bash
python -m app.cli.register_bot --name deckout --scopes read,currency:grant,currency:deduct
```

`achievement:grant` and `xp:add` are deliberately excluded: achievements are
local (§20.1) and Central runs its own activity XP module, where
double-crediting is forbidden. A registered bot name **cannot be re-registered**
(guide §2), so adding a scope later needs operator intervention.

## Layout

| Path | Doc sections |
|---|---|
| `app/db/schema.sql` | §18 — the full schema; its constraints are normative |
| `app/db/connection.py` | §18.9 migration gate, transaction helper |
| `app/content/operators.py` | §10.4 operator whitelist, categories, `BATTLE_SAFE`/`PROGRESSION` |
| `app/content/validation.py` | §10.5 validation, including the conservative summon bound |
| `app/content/versioning.py` | §10.6 two-part content keys, publish/fingerprint |
| `app/content/balance.py` | §15 constants — loaded from DB, never hardcoded |
| `app/content/seed.py` | §13.2 minimum launch set, §4.6 account initialization |
| `app/engine/battle.py` | §2.11 turn machine, §2.12 round boundary, §2.8.4 boss phases |
| `app/engine/statuses.py` | §2.5.1 two clocks, three persistence models |
| `app/engine/timed_effects.py` | §2.5.3 round-scoped modifier instances |
| `app/engine/targeting.py` | §2.8.1–2.8.3 strategies, threat model, 도발 override |
| `app/engine/enemy_ai.py` | §2.8.5 action selection and cooldowns, §2.8.6 telegraphs |
| `app/engine/deck.py` | §2.2 draw/discard, §2.7.3 atomic cursed insertion |
| `app/engine/effects.py` | §10.4.2 operator execution and suspension |
| `app/engine/map_gen.py` | §3.5.3 canonical generation, retry bound, fallback |
| `app/engine/gacha.py` | §5 pity curve, redistribution, first-pull guarantee |
| `app/engine/settlement.py` | §8.6.3 settlement, §15.10 retention, §16.2.1 step machine |
| `app/engine/lifecycle.py` | §16 states, `preparing` flow, build snapshot, CAS |
| `app/engine/rng.py` | §16.4 RNG operation journal |
| `app/central/client.py` | §1.3 capabilities, threads, edits, applied-delta economy |
| `app/central/transactions.py` | §17 direction-aware transaction machine, receipts |
| `app/central/delivery.py` | §1.3.3 delivery intents, §16.6 ordering queue |
| `app/api/server.py` | §1.3.1 `/event`, `/shutdown`, `/healthz` |
| `app/api/events.py` | §1.2 per-type parsing (five shapes, not a shared model) |
| `app/api/custom_id.py`, `gates.py`, `errors.py` | §19.2 `custom_id`, §1.3.10 five gates, §19.4 strings |
| `app/render/panels.py` | §11 two-panel battle screen, map, settlement |

## Decision markers

The doc's §0.1 provenance markers are respected throughout. 🟡 RECOMMENDED
items are implemented as written and flagged in comments; **🔴 PENDING items
were not invented**.

## What is not implemented

| Item | Why |
|---|---|
| **Card upgrade system (P-1)** | 🔴 PENDING — the owner is authoring it. §5.8's interface is honoured: `card_fragments` is per `(user_id, card_id)`, `unlocked_cards.upgrade_tier` is account-level and enters the run through the build snapshot, and §17.1 pre-registers the transaction shape. Tier count, per-tier costs and per-tier effects are absent by design. |
| **Admin/content dashboard (§10.1–10.3)** | The web CMS front-end is a separate deliverable. Its **validation layer is built** (`app/content/validation.py`) and is the same pass the loader runs, so the dashboard can be added without touching the engine. |
| **Hub screens** (`덱`/`뽑기`/`캐릭터`/`장비`/`연구`/`상점`) | The engines behind them exist (gacha, research nodes, equipment defs, transactions); the per-screen Discord component layouts are content work (§13.2), and §13.3 explicitly does not claim the UI copy as closed. |
| **Art assets** | §11's fallback path is implemented — missing art renders a rarity-tier silhouette plus the entity name, so the game never fails to render. |
| **Content beyond the launch set** | §13.2's authoring list: full character/card/enemy rosters, passive effects, equipment sets, events beyond the 8 seed, achievements beyond the 3 research-gating ones. |
| **Daily/attendance claim rules** | §13.2 — the `daily_claims` table exists; the KST boundary and missed-day rules are unspecified. |

Two naming-only 🔴 items (P-2 starter display name, P-3 run-currency label) do
not block anything: `starter_001` and `run_currency` are the stable internal
identifiers, and the display strings are placeholders.

## Two places the doc needed a judgement call

§3.5.3 step 5 says that on shuffle exhaustion the fallback assigns node types
"greedily in quota order, which always satisfies the constraints for this fixed
depth shape." For the 1/2/3/3/3/2/1 shape it does not: by the time 휴식's turn
comes in quota order the only free slots are at depth 6, adjacent to the fixed
depth-7 rest node, which violates the no-adjacent-rest rule. `map_gen.py` places
the constrained types (상점, 휴식) first and fills the rest in quota order,
preserving the intent — a deterministic fallback that always validates.

§15.1's encounter downscaling is written for **party size 2** and is applied to
exactly that. Party size 1 exists only in the tutorial world (§4.1), whose
encounters are authored directly against the solo starter (§15.9's 1–2 enemies
per node), so thinning them further would contradict that tuning.
