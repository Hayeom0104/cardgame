# [가칭] Card Game Bot — Design Document v1

## 0. Document Status

This is the **first structured design draft**, derived from collaborative design discussion between the project owner (다니엘) and Claude, building on `CardGame_Concept_Handoff_v1.md`. It supersedes several items previously marked `TBD` in the handoff doc — those changes are called out explicitly below with a `[CHANGED FROM HANDOFF v1]` tag.

Items still marked `TBD` are genuinely undecided and must not be invented by implementers. Numeric balancing values (exact costs, damage numbers, drop rates) are intentionally deferred to a later balancing pass unless explicitly stated.

**Bot name**: `TBD` — use placeholder `CardGameBot` in code/config until finalized.

**Relationship to other ARI projects**: Independent bot/project. No shared code or data with "Noobs in Card" or other existing ARI minigames, aside from the shared Central Bot integration contract.

---

## 1. Architecture

- Runs as its own **FastAPI HTTP server**, following the existing ARI minigame integration pattern (see `중앙봇_API_연동_가이드_업데이트.md`).
- Does **not** hold a Discord gateway connection or bot token. All Discord delivery is proxied through the Central Bot via `POST /event` (see §8 for event/response contract references).
- Uses `!커맨드` prefix commands in designated channels, consistent with ARI convention.
- **Image rendering**: Pillow, used for battle screens, the world map, and per-card/character/enemy illustrations (see §10).

### 1.1 Data ownership boundary [IMPORTANT — confirmed during design discussion]

The Central Bot's Economy API only models a single `balance` field (코인) and `xp`/`level`. It has **no concept** of the new game-specific currencies or entities introduced by this bot (카드 조각, 와일드카드, 카르타, 장비, 카드/캐릭터 ownership, decks, research state, etc.).

Division of responsibility:

| Data | Owner | Sync mechanism |
|---|---|---|
| 코인 (coin) balance | Central Bot | `GET /v1/users/{user_id}`, `POST /v1/currency/add` (per 중앙봇 API guide §4.3) |
| XP / level (if this bot grants activity XP) | Central Bot | `POST /v1/xp/add` |
| 카드 조각 / 와일드카드 / 카르타 balances | **This bot's own SQLite DB** | N/A — local only |
| Character/card ownership, decks, gacha state, research state, equipment | **This bot's own SQLite DB** | N/A — local only |
| Achievements (e.g. "블랙잭에서 100만 배팅" style) | Central Bot | `POST /v1/achievements/grant` or `/unlock` per 중앙봇 API guide §5 |

This bot needs its own SQLite database (`cardgame.db` or similar) separate from `central.db`, per the Central Bot's "no direct DB access" rule (중앙봇 가이드 §1).

---

## 2. Combat System

### 2.1 Turn structure

- **Individual turn order**: each of the player's party members acts on their own turn (not a unified "party turn").
- **Interleaved with enemies**: player and enemy units alternate turns one at a time (not "all players, then all enemies").
- Exact turn-order determination (e.g. speed stat vs fixed order) — `TBD`, balancing detail.

### 2.2 Card draw & hand

- Each character turn: **draw multiple cards, player selects 1 to play**.
- Unselected drawn cards are **discarded at end of turn** (not kept as a hand).
- **Draw pile exhaustion**: once a character's draw pile (that run's active deck) is exhausted, no more cards are drawn for that character (specific penalty presentation — `TBD`). No auto-reshuffle.
- Exact draw count per turn — `TBD`, balancing detail (design intent: several cards drawn, 1 chosen, per handoff doc's "player picks one of the drawn cards").

### 2.3 Resource (cost) system

- Cards have a **cost**; playing a card consumes resource.
- Resource pool is **shared across the whole party** (not per-character).
- Resource **fully refills once per round** (i.e., after all 3 player characters have taken their turn in that round — before/at the point the next round of player turns begins).

### 2.4 HP

- **Per-character individual HP** (not a shared party HP pool).
- HP **persists across map nodes within a run** (no automatic heal between nodes) — this is what makes 휴식(회복) nodes (§3) meaningful.

### 2.5 Card effect categories

- **공격 (Attack)**, **방어 (Defense)**, **버프/디버프 (Buff/Debuff)**, **회복 (Heal)**.
- Targeting: **mixed per-card** — some cards single-target (적/아군 1명), some AoE. Not a global rule; defined per card.
- **방어 (Defense) mechanic**: grants **block/shield** that resets at the start of each turn (Slay-the-Spire-style), not a permanent stat increase.
- Status effect taxonomy (burn, stun, etc.) — `TBD`, deferred to balancing/content pass.

### 2.6 Enemy composition [CHANGED FROM HANDOFF v1]

> Handoff v1 §2 stated the battle screen has "up to 3 enemy slots" as a confirmed layout. This has been **superseded**:

- Enemy count **varies by stage/node** (difficulty scaling) and has **no upper cap** — boss fights etc. may field more than 3 enemies.
- Battle screen layout must support **multi-row/grid placement** for the enemy side when enemy count is high (not a fixed 3-slot single row).
- Player side remains **up to 3 slots** (tied to party size, see §4.5).

### 2.7 Out of scope for this draft

- Enemy AI behavior patterns — `TBD`.
- Exact status effect list and mechanics — `TBD`.
- Numeric balancing (HP values, damage numbers, resource pool size) — deferred.

---

## 3. Map / Exploration System

- **로그라이크형 (roguelike)**: map is randomly generated **per run**, not a fixed persistent campaign.
- **Player actively chooses** the next node among branch options (not linear/automatic).
- **패배(defeat) → 런 전체 리셋**: losing a battle resets the entire run back to the start (no partial-progress retry).
- **HP persists across nodes** within a run (see §2.4) — this creates real risk/reward tension around 휴식 nodes.

### 3.1 Node types (confirmed list)

```
전투 (Combat)
보상 (Reward)   — see §3.2, also called 카드방/상자방/황금방
휴식 (Rest)     — heal
상점 (Shop)     — in-run shop, see §7.1
이벤트 (Event)  — content TBD
보스 (Boss)     — terminal node of the world
```

### 3.2 보상 노드 ("카드방") — run-scoped deck building

This is the roguelike "card reward room" mechanic (equivalent term: 상자방/황금방). It is **the same node type as 보상**, not a separate one.

- At a 보상 node, the game offers cards from that character's **permanently-unlocked card list** (see §5.3 — cards the player has pulled from gacha at least once, for that character or shared/universal cards).
- Selected cards are added to **that run's active deck only** — **temporary, reset when the run ends**.
- This is entirely separate from **permanent unlocking**, which happens via gacha (§5).

---

## 4. Party & Characters

### 4.1 Party size

- Party: up to 3 characters (per handoff doc §2), can be fewer.
- **Starting party size is 1**, expanding to 2 then 3.

### 4.2 Party slot unlock — via 연구 시스템 [CHANGED FROM HANDOFF v1]

- Party slot expansion is **not a run-scoped mechanic** — it is a **permanent, account-wide unlock** granted through the 연구 시스템 (Research System, §9).
- Once unlocked, all future runs start with the expanded party size.

### 4.3 Starting cards

- Every character starts (even with zero gacha pulls) with **basic default cards**: a simple attack card (평타) and a basic defense card. This guarantees a character is always playable at minimum.

### 4.4 Character rarity — Blue-Archive-style star system [CHANGED FROM HANDOFF v1 — new detail]

- Characters pull at **1★ / 2★ / 3★**.
- **성급 상승 (star rank-up)**: spend **카드 조각** to raise a character's star rank.
- **Select special characters** can rank up beyond the normal cap, up to **6★**.

---

## 5. Gacha System

### 5.1 Gacha pool composition [CHANGED FROM HANDOFF v1]

- A single gacha pull can yield **either a character or a battle card** — **characters are themselves one category of "card"** pulled from the same overall system.
- Battle cards are a mix of **character-exclusive** cards and **universal cards shared across characters**.

### 5.2 Duplicate conversion

| Duplicate result | Converts to |
|---|---|
| Duplicate character | 와일드카드 (Wildcard) |
| Duplicate card | 카드 조각 (Card Fragment) |

### 5.3 Permanent unlock vs run-scoped use

- Pulling a card from gacha **permanently marks it as "obtainable"** for that character going forward — it becomes eligible to appear as an option at 보상 nodes (§3.2) in all future runs.
- Cards never pulled do not appear as 보상 node options.
- This is independent of whether the card is currently in a run's active deck.

### 5.4 Banners

- Structure: **상시 배너 (standing/permanent banner) + 한정 픽업 배너 (rotating limited banner)**, Genshin-Impact-style.
- Each banner has its own composition of characters/cards, including featured/pickup items on limited banners.
- **최고 등급 적중 시 (top-rarity hit on limited banner)**: **50/50** — the pull has a 50% chance of being the featured pickup; a loss guarantees the next top-rarity pull is the featured pickup (standard pity-guarantee pattern).
- Whether pickup targets are characters only, or characters+cards depending on the banner — `TBD`.
- Whether standard/limited banners use separate currencies — `TBD`.

### 5.5 Gacha currency — 카르타 (Carta) [NEW]

- Dedicated premium currency, separate from 코인.
- **이름: 카르타** (from "carta" = card).
- Used for: gacha pulls, and other premium purchases (equipment, special shop items).
- Acquisition: **일일/출석 보상 (daily/attendance rewards) + 런 플레이 보상 (roguelike run rewards)**.
- Monetization (real-money purchase option) — `TBD`.

### 5.6 Skill/passive card rarity

- Separate **6-tier** rarity scale for skill/passive cards (mechanics still TBD, see §6).

### 5.7 Pity/guarantee system for standard pulls — `TBD`, deferred to balancing.

---

## 6. Passive Card System [NEW — was explicitly out-of-scope in handoff v1]

- **발동 방식 (activation)**: primarily **equipped before battle**, providing an always-on passive effect for the whole battle. Some passives additionally have **in-battle trigger conditions** (both patterns coexist; defined per-card).
- **슬롯 (slots)**: **party-shared**, not per-character. Starts at **2 slots**, expandable to a max of **4 slots** via the 연구 시스템 (§9).
- **장착 (equip duration)**: **run-scoped** — re-selected fresh each run, matching the battle-deck pattern (§3.2).
- **획득 (acquisition)**: must first be **unlocked via gacha** (§5), then actually obtained through in-game play (run rewards), mirroring the battle-card unlock→run-use pattern.
- **등급**: 6-tier rarity (§5.6).
- Detailed passive effect list/mechanics — `TBD`.

---

## 7. Shop System

Two distinct shops, different roles:

### 7.1 In-run node shop (맵의 상점 노드)

- Sells: **run-scoped consumables/temporary buffs**, and **cards usable within that run** (functionally similar to 보상 node card offers, but purchased rather than free).
- Resets/disappears at end of run (matches run-scoped deck logic in §3.2).

### 7.2 Hub shop (런 밖, 상시)

- Sells: **permanent items**, primarily **장비 (equipment, see §8)**.
- Currency likely coin and/or 카르타 — exact pricing `TBD`.

---

## 8. Equipment System [NEW — not in handoff v1]

Reference concept: Blue Archive-style equipment tier-up ("티어제").

- **획득 (acquisition)**: confirmed via **허브 상점 구매** (§7.2). Other acquisition routes (map drops, gacha) — `TBD`.
- **강화 (tier-up)**: requires a **dedicated equipment enhancement material**, distinct from card/character materials (카드 조각, 와일드카드). Exact name and acquisition source for this material — `TBD`.
- Equipment stat effects, slot count per character, equipment rarity — `TBD`.

---

## 9. Research System (연구 시스템) [NEW — not in handoff v1]

- **해금 방식**: **즉시 해금** — no timer/wait, applies as soon as the resource cost is paid (unlike typical mobile-game "research takes N hours" patterns).
- **재화**: uses **existing currencies** (코인 / 카드 조각 / 와일드카드) — no new dedicated research currency.
- **범위 (scope)**: a broad tech-tree concept covering (at minimum):
  - 파티 슬롯 확장 (party slot unlock, §4.2)
  - 패시브 카드 슬롯 확장 (passive slot unlock, §6)
  - 스탯/능력치 강화
  - 신규 노드 타입 해금
  - Full node list and exact unlock costs — `TBD`, deferred to a dedicated research-tree design pass.

---

## 10. Admin/Content Dashboard [NEW]

A web dashboard for non-engineering team members to manage game content without touching code or the Discord bot directly.

### 10.1 Scope

- Manages: **카드 (cards) / 적 (enemies) / 캐릭터 (characters)** data.
- Equipment management — not yet in scope (revisit later if needed).

### 10.2 Requirements

- **Web dashboard** (browser-based forms), not Discord slash commands or spreadsheet import.
- **Image upload**: must support uploading illustration/icon files directly through the dashboard (for card art, character portraits, enemy sprites — feeds into the Pillow rendering pipeline, §11).
- **Multi-user**: multiple team members will use this concurrently.
- **Auth**: **Discord OAuth login** (log in with Discord account, not standalone username/password).
- **Permissions**: role separation needed. Proposed default (implementer may refine): `Owner` (full read/write, user management) and `Editor` (read/write on card/enemy/character content, no user management). Exact granularity — open to adjustment.

### 10.3 Architecture note

This dashboard writes to the same local SQLite DB this bot owns (§1.1) — it is effectively a CMS front-end for that DB, likely as a separate lightweight web service (or a route group within the same FastAPI app) rather than going through Discord at all.

---

## 11. Pillow Rendering Scope

| Screen | Requirement |
|---|---|
| 전투 화면 (battle screen: unit placement, HP bars, etc.) | **Required** — combat cannot be conveyed via text alone. |
| 맵 화면 (node graph) | **Required** — the whole map rendered as a single image per update. |
| 카드 아트 (per-card illustration/icon) | **Required, unique per card** — implies a significant art-asset production requirement; flag this as a production dependency, not just an engineering task. |
| Character/enemy portraits | Implied by §10 (dashboard uploads illustrations for these) — required. |

---

## 12. Persistent vs Run-Scoped Data — Quick Reference

This distinction recurs throughout the design and is worth keeping explicit for implementation:

| Element | Scope |
|---|---|
| Character ownership, star rank | Permanent (account) |
| Card "unlocked" status (eligible for 보상 nodes) | Permanent (account) |
| 코인 / 카드 조각 / 와일드카드 / 카르타 balances | Permanent (account) |
| Equipment ownership & tier | Permanent (account) |
| Research System unlocks (party slots, passive slots, stats, node types) | Permanent (account) |
| Active battle deck (which unlocked cards are currently in the deck) | **Run-scoped** — reset on new run |
| Passive card equip selection | **Run-scoped** — reset on new run |
| In-run node shop inventory/purchases | **Run-scoped** |
| Map layout itself | **Run-scoped** (regenerated each run) |
| HP | Persists **within** a run (node to node), resets **between** runs |

---

## 13. Open TBD List (do not invent — confirm before implementing)

- Bot name.
- Exact draw count per turn; exact resource pool size and card costs (balancing).
- Draw-pile-exhaustion penalty specifics.
- Turn-order-within-round determination (speed stat vs fixed).
- Status effect taxonomy and mechanics.
- Enemy AI behavior patterns.
- Passive card effect list/mechanics (only rarity tiering — 6 tiers — is confirmed).
- Equipment stat effects, acquisition beyond hub shop, tier-up material name/source.
- Research tree: full node list, unlock costs, stat-boost specifics, which new node types it unlocks.
- Gacha: standard-pull pity/guarantee system; whether standard/limited banners use separate currencies; whether limited-banner pickup can be a card (vs character-only); exact pull cost in 카르타; whether 카르타 is purchasable with real money.
- Shop pricing (both node shop and hub shop).
- Event node (맵 node type) content.
- Admin dashboard: final permission granularity.
- All numeric balancing values project-wide.

---

## 14. Source Documents

- `CardGame_Concept_Handoff_v1.md` — original concept handoff (superseded in places, see `[CHANGED FROM HANDOFF v1]` tags above).
- `중앙봇_API_연동_가이드_업데이트 (2).md` — Central Bot integration contract this bot must follow for Discord delivery and coin/XP/achievement sync.
