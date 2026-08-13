"""§5 — the gacha system.

Three things here are load-bearing and were each repaired in a revision:

* **Pity** (§15.4, M-13): `+5.0%p` per pull past 70, peaking at 96.5% on pull
  89 so pull 90 is genuinely the hard pity, with an explicit redistribution
  rule that keeps the bands summing to exactly 100%.
* **RNG** (§5.9, M-07): a server-controlled per-transaction `commit_seed`,
  never the player-visible run seed, and never the §16.4 run journal — gacha
  happens outside any run.
* **The first-pull guarantee** (§5.10, B-05): counted by pull **results**, not
  by batch, because nothing forces a new player to use a 10-pull. Limited
  banners stay locked until it is consumed, which removes the §5.4.1 collision
  rather than ranking it.
"""

from __future__ import annotations

import json
import random
import secrets
import uuid
from dataclasses import dataclass, field

from app.content.balance import Balance
from app.db.connection import Database, utcnow

STANDARD_SCOPE = "standard_global"
LIMITED_SCOPE = "limited_global"

BAND_TOP = "top"       # 최고등급 — 3★ character / 6-tier card
BAND_MID = "mid"       # 중간등급 — 2★ character / 4–5 tier card
BAND_BASE = "base"     # 기본등급 — 1★ character / 1–3 tier card


class GachaError(RuntimeError):
    pass


@dataclass
class PullResult:
    index: int
    band: str
    kind: str                  # 'character' | 'card'
    entity_id: str
    is_duplicate: bool = False
    fragments: int = 0
    wildcards: int = 0
    forced_by_guarantee: bool = False
    won_5050: bool | None = None


@dataclass
class GachaOutcome:
    gacha_id: str
    results: list[PullResult] = field(default_factory=list)
    carta_spent: int = 0
    replayed: bool = False
    log: list[str] = field(default_factory=list)


# =====================================================================
# §15.4 pity curve and rarity redistribution
# =====================================================================
def band_probabilities(balance: Balance, pull_count: int) -> dict[str, float]:
    """Probabilities for the *next* pull, given how many have passed since the
    last 최고등급 hit.

        P_top(n) = 1.5%                                  for n <= 70
                 = min(100%, 1.5% + 5.0%p × (n − 70))    for 71 <= n <= 89
                 = 100% (hard pity)                       at n = 90

        기본 = max(0, 100 − P_top − 12)
        중간 = 100 − P_top − 기본
    """
    rates = balance.get("gacha_base_rates")
    soft_start = int(balance.get("pity_soft_start"))
    increment = float(balance.get("pity_soft_increment"))
    hard = int(balance.get("pity_hard"))
    mid_fixed = float(balance.get("pity_mid_band_fixed"))

    n = pull_count + 1     # the pull about to be resolved
    if n >= hard:
        p_top = 1.0
    elif n <= soft_start:
        p_top = float(rates[BAND_TOP])
    else:
        p_top = min(1.0, float(rates[BAND_TOP]) + increment * (n - soft_start))

    p_base = max(0.0, 1.0 - p_top - mid_fixed)
    p_mid = max(0.0, 1.0 - p_top - p_base)
    return {BAND_TOP: p_top, BAND_MID: p_mid, BAND_BASE: p_base}


def _roll_band(rng: random.Random, probabilities: dict[str, float]) -> str:
    roll = rng.random()
    upto = 0.0
    for band in (BAND_TOP, BAND_MID, BAND_BASE):
        upto += probabilities[band]
        if roll < upto:
            return band
    return BAND_BASE


# =====================================================================
# Banner access (§5.10.2)
# =====================================================================
def banner_is_available(db: Database, user_id: int, banner: dict) -> bool:
    """An account with `first_pull_guarantee_used = false` may pull ONLY on the
    standard banner. Limited banners are hidden/disabled until it is consumed.
    """
    if banner["banner_type"] == "standard":
        return True
    account = db.one("SELECT first_pull_guarantee_used FROM accounts WHERE user_id = ?",
                     (user_id,))
    return bool(account and account["first_pull_guarantee_used"])


# =====================================================================
# Pull
# =====================================================================
def pull(db: Database, balance: Balance, *, user_id: int, banner_id: str,
         pull_kind: str, content_version_id: int,
         gacha_id: str | None = None) -> GachaOutcome:
    """Resolve a pull batch as ONE local transaction (§17.5).

    A retry with the same `gacha_id` REPLAYS `results_json`; it never re-rolls.
    """
    gacha_id = gacha_id or uuid.uuid4().hex

    existing = db.one("SELECT * FROM gacha_transactions WHERE gacha_id = ?", (gacha_id,))
    if existing is not None and existing["results_json"]:
        outcome = GachaOutcome(gacha_id=gacha_id, replayed=True,
                               carta_spent=existing["carta_cost"])
        outcome.results = [PullResult(**entry)
                           for entry in json.loads(existing["results_json"])]
        return outcome

    banner = db.one(
        "SELECT * FROM banners WHERE content_version_id = ? AND banner_id = ?",
        (content_version_id, banner_id),
    )
    if banner is None:
        raise GachaError(f"banner {banner_id!r} is not defined")
    if not banner_is_available(db, user_id, banner):
        raise GachaError("limited banners are locked until the first-pull "
                         "guarantee is consumed")

    size = (int(balance.get("gacha_ten_pull_size")) if pull_kind == "ten" else 1)
    cost = int(balance.get("gacha_cost_ten") if pull_kind == "ten"
               else balance.get("gacha_cost_single"))

    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    if account is None:
        raise GachaError(f"account {user_id} does not exist")
    if account["carta"] < cost:
        # 이 검사는 UX용이다. 권위 있는 검사는 아래 트랜잭션 안에서 다시 한다 —
        # 두 번의 클릭이 여기를 나란히 통과할 수 있기 때문이다.
        raise GachaError("insufficient 카르타")

    # 1. Create the transaction row FIRST, with a cryptographically random
    #    commit_seed. All results derive from it (§5.9).
    #
    #    A row that already exists without results is a crash between the
    #    INSERT and the result write: reuse its commit_seed so the retry is a
    #    true replay rather than a re-roll, and so the same gacha_id never
    #    becomes permanently unusable.
    if existing is not None:
        commit_seed = existing["commit_seed"]
    else:
        commit_seed = secrets.token_hex(32)
        db.execute(
            "INSERT INTO gacha_transactions (gacha_id, user_id, banner_id, pull_kind, "
            "carta_cost, commit_seed, results_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 'created', ?)",
            (gacha_id, user_id, banner_id, pull_kind, cost, commit_seed, utcnow()),
        )

    outcome = GachaOutcome(gacha_id=gacha_id, carta_spent=cost)
    with db.tx() as conn:
        # 동시 클릭 방지: 잔액을 트랜잭션 안에서 다시 읽는다. 밖에서만 검사하면
        # 두 요청이 모두 통과해 카르타가 음수가 되고 §5.10 카운터가 뒤엉킨다.
        account = conn.execute("SELECT * FROM accounts WHERE user_id = ?",
                               (user_id,)).fetchone()
        if account["carta"] < cost:
            raise GachaError("insufficient 카르타")

        rng = random.Random(int(commit_seed[:16], 16))
        scope = banner["pity_scope_id"]
        pity = _pity_row(conn, user_id, scope)
        pull_count = int(pity["pull_count"])
        guarantee_pending = bool(pity["guarantee_pending"])

        first_pull_count = int(account["first_pull_results_count"])
        guarantee_used = bool(account["first_pull_guarantee_used"])
        window = int(balance.get("first_pull_guarantee_window"))
        character_seen = _owns_any_gacha_character(conn, user_id, content_version_id)

        for index in range(size):
            probabilities = band_probabilities(balance, pull_count)
            band = _roll_band(rng, probabilities)

            # §5.10.1 — result #10 is FORCED to a character when results 1..9
            # produced none; its rarity band still rolls normally.
            forced = False
            if not guarantee_used and first_pull_count + 1 >= window and not character_seen:
                forced = True

            kind = _roll_kind(rng, balance, forced=forced)
            result = _resolve_entity(
                conn, rng, balance, user_id=user_id, banner=banner, band=band,
                kind=kind, content_version_id=content_version_id,
                guarantee_pending=guarantee_pending,
            )
            result.index = index
            result.forced_by_guarantee = forced
            outcome.results.append(result)

            if result.kind == "character":
                character_seen = True

            # Pity: reset on any 최고등급 hit, otherwise advance.
            if band == BAND_TOP:
                pull_count = 0
                if banner["banner_type"] == "limited":
                    guarantee_pending = not bool(result.won_5050)
            else:
                pull_count += 1

            if not guarantee_used:
                first_pull_count = min(window, first_pull_count + 1)
                if first_pull_count >= window or (character_seen and forced):
                    guarantee_used = True

        # 3. Persist results together with the deduction, pity update, 50/50
        #    update, duplicate conversion and ownership inserts — ONE local
        #    transaction (§17.5).
        conn.execute(
            "UPDATE accounts SET carta = carta - ?, first_pull_results_count = ?, "
            "first_pull_guarantee_used = ?, updated_at = ? WHERE user_id = ?",
            (cost, first_pull_count, int(guarantee_used), utcnow(), user_id),
        )
        conn.execute(
            "UPDATE gacha_pity SET pull_count = ?, guarantee_pending = ? "
            "WHERE user_id = ? AND pity_scope_id = ?",
            (pull_count, int(guarantee_pending), user_id, scope),
        )
        conn.execute(
            "UPDATE gacha_transactions SET results_json = ?, status = 'completed' "
            "WHERE gacha_id = ?",
            (json.dumps([result.__dict__ for result in outcome.results],
                        ensure_ascii=False), gacha_id),
        )
    return outcome


def _pity_row(conn, user_id: int, scope: str):
    conn.execute(
        "INSERT OR IGNORE INTO gacha_pity (user_id, pity_scope_id, pull_count, "
        "guarantee_pending) VALUES (?, ?, 0, 0)",
        (user_id, scope),
    )
    return conn.execute(
        "SELECT * FROM gacha_pity WHERE user_id = ? AND pity_scope_id = ?",
        (user_id, scope),
    ).fetchone()


def _roll_kind(rng: random.Random, balance: Balance, *, forced: bool) -> str:
    """3:7 character:card within every band. The §5.10 guarantee overrides only
    the character/card axis, never the rarity band."""
    if forced:
        return "character"
    return "character" if rng.random() < float(balance.get("gacha_character_split")) else "card"


def _resolve_entity(conn, rng: random.Random, balance: Balance, *, user_id: int,
                    banner, band: str, kind: str, content_version_id: int,
                    guarantee_pending: bool) -> PullResult:
    won_5050: bool | None = None

    if (band == BAND_TOP and banner["banner_type"] == "limited"
            and banner["pickup_type"]):
        # §5.4.1 — won or guaranteed yields the pickup target.
        if guarantee_pending:
            won_5050 = True
        else:
            won_5050 = rng.random() < float(balance.get("limited_5050_rate"))
        if won_5050 and banner["pickup_type"] == kind:
            entity_id = banner["pickup_target_id"]
            return _grant(conn, balance, user_id, kind, entity_id, band,
                          content_version_id, won_5050=won_5050)

    pool = _pool_for(conn, content_version_id, kind, band,
                     exclude_id=banner["pickup_target_id"] if won_5050 is False else None)
    if not pool:
        pool = _pool_for(conn, content_version_id, kind, band)
    if not pool:
        raise GachaError(f"no {kind} content available for band {band!r}")
    entity_id = pool[rng.randrange(len(pool))]
    return _grant(conn, balance, user_id, kind, entity_id, band,
                  content_version_id, won_5050=won_5050)


def _pool_for(conn, content_version_id: int, kind: str, band: str,
              exclude_id: str | None = None) -> list[str]:
    if kind == "character":
        star = {BAND_TOP: 3, BAND_MID: 2, BAND_BASE: 1}[band]
        rows = conn.execute(
            "SELECT character_id FROM characters WHERE content_version_id = ? "
            "AND in_gacha_pool = 1 AND is_retired = 0 AND base_rarity = ? "
            "ORDER BY character_id",
            (content_version_id, star),
        ).fetchall()
        return [row["character_id"] for row in rows
                if row["character_id"] != exclude_id]

    tiers = {BAND_TOP: (6,), BAND_MID: (4, 5), BAND_BASE: (1, 2, 3)}[band]
    placeholders = ",".join("?" * len(tiers))
    rows = conn.execute(
        f"SELECT card_id FROM cards WHERE content_version_id = ? AND is_retired = 0 "
        f"AND rarity_tier IN ({placeholders}) ORDER BY card_id",
        (content_version_id, *tiers),
    ).fetchall()
    return [row["card_id"] for row in rows if row["card_id"] != exclude_id]


def _grant(conn, balance: Balance, user_id: int, kind: str, entity_id: str,
           band: str, content_version_id: int,
           won_5050: bool | None) -> PullResult:
    """Ownership insert, or §5.2 duplicate conversion.

    A duplicate character yields **both** that character's 캐릭터 조각 *and*
    와일드카드 (🟡 R-4): fragments alone would leave 와일드카드 with no source
    while star-up still requires it.
    """
    result = PullResult(index=0, band=band, kind=kind, entity_id=entity_id,
                        won_5050=won_5050)

    if kind == "character":
        existing = conn.execute(
            "SELECT star_rank FROM owned_characters WHERE user_id = ? AND character_id = ?",
            (user_id, entity_id),
        ).fetchone()
        if existing is None:
            star = conn.execute(
                "SELECT base_rarity FROM characters WHERE content_version_id = ? "
                "AND character_id = ?", (content_version_id, entity_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO owned_characters (user_id, character_id, star_rank, "
                "acquired_at) VALUES (?, ?, ?, ?)",
                (user_id, entity_id, star["base_rarity"] if star else 1, utcnow()),
            )
            return result

        yields = balance.get("duplicate_character_yield")
        entry = yields.get(str(existing["star_rank"]), yields["1"])
        result.is_duplicate = True
        result.fragments = int(entry["fragments"])
        result.wildcards = int(entry["wildcards"])
        conn.execute(
            "INSERT INTO character_fragments (user_id, character_id, amount) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, character_id) DO UPDATE SET "
            "amount = amount + excluded.amount",
            (user_id, entity_id, result.fragments),
        )
        conn.execute(
            "UPDATE accounts SET wildcards = wildcards + ? WHERE user_id = ?",
            (result.wildcards, user_id),
        )
        return result

    existing = conn.execute(
        "SELECT card_id FROM unlocked_cards WHERE user_id = ? AND card_id = ?",
        (user_id, entity_id),
    ).fetchone()
    if existing is None:
        # §5.3 — pulling a card permanently marks it obtainable at 보상 nodes.
        conn.execute(
            "INSERT INTO unlocked_cards (user_id, card_id, upgrade_tier, unlocked_at) "
            "VALUES (?, ?, 0, ?)", (user_id, entity_id, utcnow()),
        )
        return result

    tier = conn.execute(
        "SELECT rarity_tier FROM cards WHERE content_version_id = ? AND card_id = ?",
        (content_version_id, entity_id),
    ).fetchone()
    amounts = balance.get("duplicate_card_yield")
    result.is_duplicate = True
    result.fragments = int(amounts.get(str(tier["rarity_tier"] if tier else 1), 5))
    conn.execute(
        "INSERT INTO card_fragments (user_id, card_id, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, card_id) DO UPDATE SET amount = amount + excluded.amount",
        (user_id, entity_id, result.fragments),
    )
    return result


def _owns_any_gacha_character(conn, user_id: int, content_version_id: int) -> bool:
    """Does the account hold a character it actually pulled?

    The starter is excluded from every pool (§5.1), so it must not satisfy the
    §5.10 guarantee — otherwise the guarantee would never fire.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM owned_characters oc JOIN characters c "
        "ON c.character_id = oc.character_id AND c.content_version_id = ? "
        "WHERE oc.user_id = ? AND c.in_gacha_pool = 1",
        (content_version_id, user_id),
    ).fetchone()
    return int(row["n"]) > 0
