"""§3 node resolution and the §16.2 run loop.

This is what turns a chosen map node into a screen: the lifecycle declares the
transitions, and this module performs them.

    node_resolution ──전투──▶ battle
    node_resolution ──보스──▶ boss_battle
    node_resolution ──보상──▶ reward_selection
    node_resolution ──이벤트──▶ event_choice
    node_resolution ──상점──▶ shop
    node_resolution ──휴식──▶ map_navigation          (heal applied immediately)

Every roll here goes through the §16.4 journal, so `node_resolution` recovery
re-runs resolution and reproduces the original encounter, shop stock, or event
(§16.8) rather than rolling something new.
"""

from __future__ import annotations

import json
import logging
import uuid

from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import achievements as ach
from app.engine import battle as bt
from app.engine import deck
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import settlement as sl
from app.engine import units as un
from app.engine.rng import JournaledRng, key_encounter
from app.engine.stats import NEUTRAL_ELEMENT

logger = logging.getLogger(__name__)

CHOICE_REWARD = "reward_card"
CHOICE_EVENT = "event_branch"


class NodeError(RuntimeError):
    pass


# =====================================================================
# Entry point
# =====================================================================
def resolve_node(db: Database, balance: Balance, rng: JournaledRng, *,
                 run_id: int, node_index: int) -> dict:
    """Set up the chosen node and move the run into the state it needs.

    Idempotent through the journal and through `pending_choices`: re-running it
    after a crash rebuilds the same screen.
    """
    run = _run(db, run_id)
    node = db.one(
        "SELECT * FROM run_nodes WHERE run_id = ? AND node_index = ?",
        (run_id, node_index),
    )
    if node is None:
        raise NodeError(f"node {node_index} does not exist in run {run_id}")

    node_type = node["node_type"]
    if node_type == map_gen.COMBAT:
        return _start_battle(db, balance, rng, run, node, kind="normal")
    if node_type == map_gen.BOSS:
        return _start_battle(db, balance, rng, run, node, kind="boss")
    if node_type == map_gen.REST:
        return _rest(db, balance, run, node)
    if node_type == map_gen.REWARD:
        return _offer_reward(db, balance, rng, run, node)
    if node_type == map_gen.SHOP:
        return _open_shop(db, balance, rng, run, node)
    if node_type == map_gen.EVENT:
        return _open_event(db, balance, rng, run, node)
    raise NodeError(f"unsupported node type {node_type!r}")


def _run(db: Database, run_id: int):
    row = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if row is None:
        raise NodeError(f"run {run_id} does not exist")
    return row


def _set_state(db: Database, run_id: int, state: str) -> None:
    db.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
               (state, utcnow(), run_id))


# =====================================================================
# 전투 / 보스
# =====================================================================
def _start_battle(db: Database, balance: Balance, rng: JournaledRng, run, node,
                  *, kind: str) -> dict:
    """Roll an encounter for this node and materialize the battle.

    A tutorial retry re-enters here, and `create_battle` inserts a NEW row at
    attempt_no + 1 (§3.4.2) — the previous row is retained for telemetry.
    """
    encounter_id = _pick_encounter(db, rng, run, node["node_index"], kind)
    battle_id = enc.create_battle(
        db, balance, run_id=run["run_id"], node_index=node["node_index"],
        encounter_id=encounter_id, content_version_id=run["content_version_id"],
        is_boss=(kind == "boss"),
    )
    engine = bt.build_engine(
        db, balance, battle_id=battle_id, run_id=run["run_id"],
        content_version_id=run["content_version_id"], rng=rng,
    )
    engine.start()
    _set_state(db, run["run_id"],
               lc.BOSS_BATTLE if kind == "boss" else lc.BATTLE)
    return {"screen": "battle", "battle_id": battle_id,
            "encounter_id": encounter_id, "is_boss": kind == "boss"}


def _pick_encounter(db: Database, rng: JournaledRng, run, node_index: int,
                    kind: str) -> str:
    candidates = [
        row["encounter_id"]
        for row in db.query(
            "SELECT encounter_id FROM encounters WHERE content_version_id = ? "
            "AND world_id = ? AND kind = ? ORDER BY encounter_id",
            (run["content_version_id"], run["world_id"], kind),
        )
    ]
    if not candidates:
        raise NodeError(
            f"world {run['world_id']!r} authors no {kind} encounter — §10.5 "
            "should have rejected this content"
        )
    return rng.choice(key_encounter(node_index), candidates)


# =====================================================================
# 휴식
# =====================================================================
def _rest(db: Database, balance: Balance, run, node) -> dict:
    """§15.7 — 30% of max HP, deliberately not a full heal (50% in the tutorial).

    HP persists across nodes within a run (§2.4), so this is the only routine
    way to get it back.
    """
    fraction = float(balance.get(
        "rest_heal_pct_tutorial" if run["is_tutorial"] else "rest_heal_pct"))
    healed: list[dict] = []
    with db.tx() as conn:
        for member in conn.execute(
            "SELECT * FROM run_characters WHERE run_id = ? ORDER BY party_slot",
            (run["run_id"],),
        ).fetchall():
            if member["hp_current"] <= 0:
                continue      # a fallen member is not revived by a rest
            amount = int(member["hp_max"] * fraction)
            conn.execute(
                "UPDATE run_characters SET hp_current = MIN(hp_max, hp_current + ?) "
                "WHERE run_id = ? AND party_slot = ?",
                (amount, run["run_id"], member["party_slot"]),
            )
            healed.append({"party_slot": member["party_slot"], "healed": amount})
    _set_state(db, run["run_id"], lc.MAP_NAVIGATION)
    return {"screen": "map", "healed": healed}


# =====================================================================
# 보상
# =====================================================================
def _offer_reward(db: Database, balance: Balance, rng: JournaledRng, run,
                  node) -> dict:
    """§3.2 — offer cards from the account's permanently-unlocked list.

    The offer is persisted as a `pending_choices` row so a restart re-renders
    the *same* three cards, not a fresh roll (M-06).
    """
    existing = _open_choice(db, run["run_id"])
    if existing is not None:
        _set_state(db, run["run_id"], lc.REWARD_SELECTION)
        return {"screen": "reward", "choice_id": existing["choice_id"],
                "options": json.loads(existing["options_json"]), "replayed": True}

    count = int(balance.get("reward_cards_offered"))
    weights = balance.get("reward_rarity_weights")
    # Only cards SOME party member can legally play are eligible: an offer with
    # no legal recipient could never be taken, and in a solo run all three
    # options could otherwise be unpickable.
    pool = [
        dict(row) for row in db.query(
            "SELECT c.card_id, c.name, c.element, c.rarity_tier FROM unlocked_cards uc "
            "JOIN cards c ON c.card_id = uc.card_id AND c.content_version_id = ? "
            "WHERE uc.user_id = ? AND c.is_retired = 0 ORDER BY c.card_id",
            (run["content_version_id"], run["user_id"]),
        )
    ]
    pool = [entry for entry in pool
            if _legal_recipients(db, run, entry["element"])]
    if not pool:
        _set_state(db, run["run_id"], lc.MAP_NAVIGATION)
        return {"screen": "map", "reason": "no playable card to offer"}

    cuts = balance.get("reward_rarity_band_max_tier")

    def band_of(tier: int) -> str:
        if tier <= int(cuts["low"]):
            return "low"
        return "mid" if tier <= int(cuts["mid"]) else "high"

    op_key = f"node:{node['node_index']}:reward"
    # §15.4 — a 보상 node also pays 탐험 자금 and rolls its own (higher) drop
    # rates. Both go through the journal so a restart does not pay twice.
    node_income = _reward_node_income(db, balance, rng, run, node["node_index"])

    options: list[dict] = []
    remaining = list(pool)
    for index in range(min(count, len(remaining))):
        picked = rng.weighted_choice(
            f"{op_key}:{index}", remaining,
            [float(weights[band_of(entry["rarity_tier"])]) for entry in remaining],
        )
        remaining = [entry for entry in remaining
                     if entry["card_id"] != picked["card_id"]]
        options.append({
            "card_id": picked["card_id"],
            "name": picked["name"],
            "element": picked["element"],
            "rarity_tier": picked["rarity_tier"],
            # §3.2 — decks are per-character, so the recipient is a required
            # step. Only characters that can legally play the card are offered.
            "recipients": _legal_recipients(db, run, picked["element"]),
        })

    choice_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO pending_choices (choice_id, run_id, node_index, choice_type, "
        "options_json, remaining_operators_json, operator_cursor, status, "
        "rng_op_key, created_at) VALUES (?, ?, ?, ?, ?, '[]', 0, 'open', ?, ?)",
        (choice_id, run["run_id"], node["node_index"], CHOICE_REWARD,
         json.dumps(options, ensure_ascii=False), op_key, utcnow()),
    )
    _set_state(db, run["run_id"], lc.REWARD_SELECTION)
    # §3.2 — a "안 받기" option is mandatory. Without it deck size only grows
    # and deck-thinning becomes impossible.
    return {"screen": "reward", "choice_id": choice_id, "options": options,
            "skip_available": True, **node_income}


def _reward_node_income(db: Database, balance: Balance, rng: JournaledRng, run,
                        node_index: int) -> dict:
    """§15.4 — 보상 node 탐험 자금 (20-40) and the higher reward-node drop rates.

    Keyed by node index, so the replay path in `_offer_reward` never re-pays.
    """
    op_key = f"node:{node_index}:reward_income"
    band = balance.get("run_currency_income")["reward_node"]
    currency = rng.randint(f"{op_key}:currency", int(band[0]), int(band[1]))
    rates = balance.get("drop_rates")

    drops: list[dict] = []
    with db.tx() as conn:
        conn.execute(
            "UPDATE runs SET run_currency = run_currency + ? WHERE run_id = ?",
            (currency, run["run_id"]))
        if rng.chance(f"{op_key}:equip", float(rates["equipment_reward"])):
            drops.append(_drop_equipment(db, rng, run, op_key))
        if rng.chance(f"{op_key}:stone", float(rates["stone_reward"])):
            low, high = rates["stone_reward_amount"]
            amount = rng.randint(f"{op_key}:stone:n", int(low), int(high))
            drops.append(_drop_stone(db, run, amount))
    return {"run_currency": currency, "drops": drops}


def _legal_recipients(db: Database, run, element: str) -> list[int]:
    """무속성 cards may go to anyone; elemental cards only to a matching
    character (§2.10)."""
    members = db.query(
        "SELECT rc.party_slot, ch.element FROM run_characters rc JOIN characters ch "
        "ON ch.character_id = rc.character_id AND ch.content_version_id = ? "
        "WHERE rc.run_id = ? ORDER BY rc.party_slot",
        (run["content_version_id"], run["run_id"]),
    )
    if element == NEUTRAL_ELEMENT:
        return [row["party_slot"] for row in members]
    return [row["party_slot"] for row in members if row["element"] == element]


def choose_reward(db: Database, run_id: int, *, choice_id: str,
                  card_id: str | None, party_slot: int | None) -> dict:
    """Apply a 보상 pick, or resolve the mandatory skip.

    Selected cards join **that run's deck only** (§3.2) — the account's
    unlocked list is untouched.
    """
    choice = db.one(
        "SELECT * FROM pending_choices WHERE choice_id = ? AND run_id = ? "
        "AND status = 'open'", (choice_id, run_id))
    if choice is None:
        raise NodeError("no open reward choice for this run")

    options = json.loads(choice["options_json"])
    if card_id is not None:
        option = next((entry for entry in options if entry["card_id"] == card_id), None)
        if option is None:
            raise NodeError("card was not offered")
        if party_slot is None or party_slot not in option["recipients"]:
            raise NodeError("recipient cannot legally play this card")

    with db.tx() as conn:
        if card_id is not None:
            deck.add_card(db, run_id, party_slot, card_id)
        conn.execute(
            "UPDATE pending_choices SET status = 'resolved', selected_option = ? "
            "WHERE choice_id = ?",
            (json.dumps({"card_id": card_id, "party_slot": party_slot},
                        ensure_ascii=False), choice_id),
        )
        conn.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                     (lc.MAP_NAVIGATION, utcnow(), run_id))
    return {"screen": "map", "taken": card_id, "party_slot": party_slot}


# =====================================================================
# 상점
# =====================================================================
def _open_shop(db: Database, balance: Balance, rng: JournaledRng, run,
               node) -> dict:
    """§7.1 — 4-6 one-time rows generated at node resolution.

    The shop sells **cards and immediate-effect services only** (B-16): no
    storage or use-flow exists for consumables or buffs anywhere in the schema.
    Payment is in 탐험 자금, never 코인 — 코인 would mean a Central round-trip
    plus a §17 transaction inside the 2.0s interaction budget on every purchase.
    """
    existing = db.query(
        "SELECT * FROM run_shop_items WHERE run_id = ? AND node_index = ? "
        "ORDER BY item_index", (run["run_id"], node["node_index"]))
    if existing:
        _set_state(db, run["run_id"], lc.SHOP)
        return {"screen": "shop", "items": [dict(row) for row in existing],
                "replayed": True}

    low, high = balance.get("run_shop_stock")
    op_key = f"node:{node['node_index']}:shop"
    stock_size = rng.randint(f"{op_key}:size", int(low), int(high))

    # Stock only cards some party member can legally play (§2.10) — an
    # unplayable purchase would take 탐험 자금 for a card that can never be
    # drawn into a legal hand.
    cards = [
        dict(row) for row in db.query(
            "SELECT c.card_id, c.name, c.element, c.rarity_tier FROM unlocked_cards uc "
            "JOIN cards c ON c.card_id = uc.card_id AND c.content_version_id = ? "
            "WHERE uc.user_id = ? AND c.is_retired = 0 ORDER BY c.card_id",
            (run["content_version_id"], run["user_id"]))
    ]
    cards = [entry for entry in cards
             if _legal_recipients(db, run, entry["element"])]
    card_price = balance.get("run_shop_card_price")
    effect_price = balance.get("run_shop_effect_price")

    items = []
    for index in range(stock_size):
        # Alternate cards and immediate effects so a shop is never all one kind.
        if cards and index % 2 == 0:
            picked = rng.choice(f"{op_key}:{index}:card", cards)
            price = rng.randint(f"{op_key}:{index}:price",
                                int(card_price[0]), int(card_price[1]))
            item_ref = json.dumps(
                {"kind": "card", "card_id": picked["card_id"],
                 "name": picked["name"], "element": picked["element"],
                 "recipients": _legal_recipients(db, run, picked["element"])},
                ensure_ascii=False)
        else:
            effect = rng.choice(f"{op_key}:{index}:effect",
                                _immediate_effects(balance))
            price = rng.randint(f"{op_key}:{index}:price",
                                int(effect_price[0]), int(effect_price[1]))
            item_ref = json.dumps(effect, ensure_ascii=False)
        db.execute(
            "INSERT INTO run_shop_items (run_id, node_index, item_index, item_ref, "
            "price, purchased) VALUES (?, ?, ?, ?, ?, 0)",
            (run["run_id"], node["node_index"], index, item_ref, price),
        )
        items.append({"item_index": index, "item_ref": item_ref, "price": price,
                      "purchased": 0})

    _set_state(db, run["run_id"], lc.SHOP)
    return {"screen": "shop", "items": items}


def _immediate_effects(balance: Balance) -> list[dict]:
    """§7.1 — 구매 즉시 해소되고 아무것도 남기지 않는 효과들.

    회복량은 `config/06_경제.toml` 의 `run_shop_heal_pct` 에서 읽는다.
    """
    return [
        {"kind": "effect", "op": "heal",
         "value": float(balance.get("run_shop_heal_pct")), "name": "HP 회복"},
        {"kind": "effect", "op": "remove_curse", "value": 1,
         "name": "저주받은 카드 1장 제거"},
        {"kind": "effect", "op": "remove_card", "value": 1,
         "name": "덱에서 카드 1장 제거"},
        {"kind": "effect", "op": "next_battle_resource", "value": 1,
         "name": "다음 전투 자원 +1"},
    ]


def buy_shop_item(db: Database, rng: JournaledRng, run_id: int, *,
                  node_index: int, item_index: int,
                  party_slot: int | None = None) -> dict:
    """One purchase. The shop loop is a self-transition (§16.2), so the run
    stays in `shop` afterwards.

    Each row is **one-time stock** (C-08): v6.2's "any number of times" meant
    *across listings*, which contradicted the boolean `purchased` column.
    """
    run = _run(db, run_id)
    item = db.one(
        "SELECT * FROM run_shop_items WHERE run_id = ? AND node_index = ? "
        "AND item_index = ?", (run_id, node_index, item_index))
    if item is None:
        raise NodeError("no such shop listing")
    if item["purchased"]:
        raise NodeError("이미 구매한 상품입니다.")
    if run["run_currency"] < item["price"]:
        raise NodeError("재화가 부족합니다.")

    payload = json.loads(item["item_ref"])
    with db.tx() as conn:
        conn.execute(
            "UPDATE runs SET run_currency = run_currency - ? WHERE run_id = ?",
            (item["price"], run_id))
        conn.execute(
            "UPDATE run_shop_items SET purchased = 1 WHERE run_id = ? "
            "AND node_index = ? AND item_index = ?",
            (run_id, node_index, item_index))

        if payload["kind"] == "card":
            # Decks are per-character (§3.2), so the recipient must be one that
            # can legally play the card — never a bare default.
            legal = payload.get("recipients") or []
            slot = party_slot if party_slot in legal else (legal[0] if legal else None)
            if slot is None:
                raise NodeError("recipient cannot legally play this card")
            deck.add_card(db, run_id, slot, payload["card_id"])
        else:
            _apply_immediate_effect(db, rng, run, payload)

    return {"screen": "shop", "purchased": payload,
            "run_currency": _run(db, run_id)["run_currency"]}


def _apply_immediate_effect(db: Database, rng: JournaledRng, run,
                            payload: dict) -> None:
    run_id = run["run_id"]
    op = payload["op"]
    if op == "heal":
        for member in db.query(
            "SELECT * FROM run_characters WHERE run_id = ?", (run_id,)):
            if member["hp_current"] <= 0:
                continue
            db.execute(
                "UPDATE run_characters SET hp_current = MIN(hp_max, hp_current + ?) "
                "WHERE run_id = ? AND party_slot = ?",
                (int(member["hp_max"] * float(payload["value"])), run_id,
                 member["party_slot"]))
    elif op == "remove_curse":
        cursed = deck.cursed_cards_in_deck(db, run_id)
        if cursed:
            deck.remove_card(db, cursed[0]["card_instance_id"])
    elif op == "remove_card":
        rows = db.query(
            "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
            "AND is_cursed = 0 ORDER BY card_instance_id", (run_id,))
        if rows:
            picked = rng.sample(f"shop:{run_id}:remove:{len(rows)}",
                                [row["card_instance_id"] for row in rows], 1)
            deck.remove_card(db, picked[0])
    elif op == "next_battle_resource":
        # Nothing persists a cross-battle buff (§7.1 narrowed the MVP), so this
        # lands on the current battle if one exists and is otherwise a no-op.
        battle = db.one(
            "SELECT battle_id FROM battles WHERE run_id = ? AND state = 'active' "
            "ORDER BY battle_id DESC LIMIT 1", (run_id,))
        if battle is not None:
            db.execute(
                "UPDATE battles SET party_resource_current = "
                "party_resource_current + ? WHERE battle_id = ?",
                (int(payload["value"]), battle["battle_id"]))


def leave_shop(db: Database, run_id: int) -> dict:
    """나가기 — the loop's only other exit is every row purchased or unaffordable."""
    _set_state(db, run_id, lc.MAP_NAVIGATION)
    return {"screen": "map"}


# =====================================================================
# 이벤트
# =====================================================================
def _open_event(db: Database, balance: Balance, rng: JournaledRng, run,
                node) -> dict:
    """§3.3 — every event outcome is persisted before it is presented."""
    existing = _open_choice(db, run["run_id"])
    if existing is not None:
        _set_state(db, run["run_id"], lc.EVENT_CHOICE)
        return {"screen": "event", "choice_id": existing["choice_id"],
                "options": json.loads(existing["options_json"]), "replayed": True}

    events = db.query(
        "SELECT * FROM events WHERE content_version_id = ? ORDER BY event_id",
        (run["content_version_id"],))
    if not events:
        _set_state(db, run["run_id"], lc.MAP_NAVIGATION)
        return {"screen": "map", "reason": "no events authored"}

    op_key = f"node:{node['node_index']}:event"
    picked = rng.choice(op_key, [row["event_id"] for row in events])
    event = next(row for row in events if row["event_id"] == picked)
    branches = json.loads(event["branches_json"])

    options = {
        "event_id": event["event_id"],
        "name": event["name"],
        "interaction_kind": event["interaction_kind"],
        "combat_link": event["combat_link"],
        "branches": [{"index": index, "label": branch.get("label", "")}
                     for index, branch in enumerate(branches)],
    }
    choice_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO pending_choices (choice_id, run_id, node_index, choice_type, "
        "options_json, remaining_operators_json, operator_cursor, status, "
        "rng_op_key, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'open', ?, ?)",
        (choice_id, run["run_id"], node["node_index"], CHOICE_EVENT,
         json.dumps(options, ensure_ascii=False),
         json.dumps(branches, ensure_ascii=False), op_key, utcnow()),
    )
    _set_state(db, run["run_id"], lc.EVENT_CHOICE)
    return {"screen": "event", "choice_id": choice_id, "options": options}


def choose_event_branch(db: Database, balance: Balance, rng: JournaledRng,
                        run_id: int, *, choice_id: str, branch_index: int) -> dict:
    """Execute one branch's operator list.

    A branch whose last operator is a TERMINAL_STATE_TRANSITION (events 4 매복
    and 8 수색 실패) transitions the lifecycle instead of returning to the map
    (§3.3.1, §10.4.1).
    """
    from app.engine import effects as fx
    from app.engine import statuses as st
    from app.engine import targeting as tg

    run = _run(db, run_id)
    choice = db.one(
        "SELECT * FROM pending_choices WHERE choice_id = ? AND run_id = ? "
        "AND status = 'open'", (choice_id, run_id))
    if choice is None:
        raise NodeError("no open event choice for this run")

    branches = json.loads(choice["remaining_operators_json"])
    if not 0 <= branch_index < len(branches):
        raise NodeError("no such event branch")
    effects = branches[branch_index].get("effects", [])

    # Close the branch choice BEFORE running its operators. A branch may contain
    # a PENDING_CHOICE operator (봉인된 제단's cleanse, 수상한 행상's recipient
    # pick), which persists its own row — and §16.5 permits exactly one open
    # `pending_choices` row per run.
    db.execute(
        "UPDATE pending_choices SET status = 'resolved', selected_option = ? "
        "WHERE choice_id = ?", (str(branch_index), choice_id))

    version = run["content_version_id"]
    ctx = fx.EffectContext(
        db=db, run_id=run_id, battle_id=None, round_no=0, balance=balance,
        status_registry=st.StatusRegistry(db, version),
        strategy_registry=tg.StrategyRegistry(db, version),
        content_version_id=version, rng=rng, actor=None, targets=[],
        host_context=fx.ops.CTX_EVENT,
    )
    outcome = fx.execute_effects(effects, ctx)

    if outcome.suspended:
        # §10.4.2 — the list persisted its cursor and is waiting on a nested
        # submission. The run stays in event_choice until it resumes.
        return {"screen": "event_choice", "suspended": True,
                "choice_id": outcome.choice_id,
                "operator": outcome.suspend_operator}

    if outcome.terminal_transition:
        params = outcome.terminal_transition["params"]
        node = db.one("SELECT * FROM run_nodes WHERE run_id = ? AND node_index = ?",
                      (run_id, choice["node_index"]))
        battle_id = enc.create_battle(
            db, balance, run_id=run_id, node_index=node["node_index"],
            encounter_id=params["encounter_id"], content_version_id=version)
        engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                                 content_version_id=version, rng=rng)
        engine.start()
        _set_state(db, run_id, lc.BATTLE)
        return {"screen": "battle", "battle_id": battle_id,
                "from_event": True}

    _set_state(db, run_id, lc.MAP_NAVIGATION)
    return {"screen": "map", "log": outcome.log}


def resume_pending_choice(db: Database, run_id: int, *,
                          selection: int | None) -> dict:
    """§10.4.2 step 3 — resume a suspended operator list at its cursor.

    The seed content's only in-run PENDING_CHOICE is `remove_cursed_card`
    (§2.7.4's cleanse), which needs the chosen card instance; anything after it
    in the list resumes normally.
    """
    choice = _open_choice(db, run_id)
    if choice is None:
        raise NodeError("no open choice to resume")

    remaining = json.loads(choice["remaining_operators_json"] or "[]")
    if selection is not None and choice["choice_type"] == "remove_cursed_card":
        owned = db.one(
            "SELECT card_instance_id FROM run_deck_cards WHERE card_instance_id = ? "
            "AND run_id = ? AND is_cursed = 1", (selection, run_id))
        if owned is None:
            raise NodeError("that cursed card is not in this run's deck")
        deck.remove_card(db, selection)

    with db.tx() as conn:
        conn.execute(
            "UPDATE pending_choices SET status = 'resolved', selected_option = ? "
            "WHERE choice_id = ?",
            (str(selection) if selection is not None else "none",
             choice["choice_id"]))
        # Operators after the suspending one resume at the cursor; the seed
        # content has none, so this is a hand-off point rather than a loop.
        conn.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                     (lc.MAP_NAVIGATION, utcnow(), run_id))
    return {"screen": "map", "resumed": len(remaining), "selection": selection}


def _open_choice(db: Database, run_id: int):
    return db.one(
        "SELECT * FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (run_id,))


# =====================================================================
# Battle conclusion — §16.2
# =====================================================================
def conclude_battle(db: Database, balance: Balance, rng: JournaledRng, *,
                    run_id: int, battle_id: int) -> dict:
    """Route a finished battle to its next state.

        battle      ──victory──▶ post_battle
        boss_battle ──victory──▶ run_settlement     (§3.6: one world = one run)
        battle | boss_battle ──defeat, is_tutorial = 0──▶ run_settlement
        battle | boss_battle ──defeat, is_tutorial = 1──▶ node_resolution
    """
    run = _run(db, run_id)
    battle = db.one("SELECT * FROM battles WHERE battle_id = ?", (battle_id,))
    if battle is None:
        raise NodeError(f"battle {battle_id} does not exist")
    if battle["state"] == bt.BATTLE_ACTIVE:
        raise NodeError("battle has not finished")

    is_boss = run["state"] == lc.BOSS_BATTLE
    if battle["state"] == bt.BATTLE_LOST:
        return _handle_defeat(db, balance, rng, run, battle)

    if is_boss:
        # Clearing the world's boss ALWAYS ends the run and unlocks the next
        # world at the account level. v6.2's "if a further world exists" branch
        # is deleted (§3.6, B-09).
        _set_state(db, run_id, lc.NODE_RESOLUTION)   # legal edge into settlement
        sl.enter_settlement(db, run_id, target_state=sl.RUN_COMPLETED,
                            end_reason="보스 처치")
        report = sl.advance_settlement(
            db, balance, rng, run_id=run_id,
            content_version_id=run["content_version_id"])
        return {"screen": "settlement", "report": report, "cleared": True}

    _set_state(db, run_id, lc.POST_BATTLE)
    rewards = _post_battle_rewards(db, balance, rng, run, battle)
    _set_state(db, run_id, lc.MAP_NAVIGATION)
    return {"screen": "map", "rewards": rewards}


def _handle_defeat(db: Database, balance: Balance, rng: JournaledRng, run,
                   battle) -> dict:
    run_id = run["run_id"]
    if run["is_tutorial"]:
        # §3.4.1 — defeat does not end the tutorial run. The party is restored
        # and the same node retried at attempt_no + 1; the previous battle row
        # is retained for telemetry and never destructively reset.
        with db.tx() as conn:
            conn.execute(
                "UPDATE run_characters SET hp_current = hp_max WHERE run_id = ?",
                (run_id,))
            conn.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                         (lc.NODE_RESOLUTION, utcnow(), run_id))

        # Resolve straight through rather than parking in node_resolution: no
        # component is rendered for that state, so a player left there would
        # have nothing to click.
        retry = resolve_node(db, balance, rng, run_id=run_id,
                             node_index=battle["node_index"])
        return {**retry, "tutorial_retry": True,
                "node_index": battle["node_index"],
                "next_attempt": battle["attempt_no"] + 1}

    sl.enter_settlement(db, run_id, target_state=sl.RUN_DEFEATED,
                        end_reason="파티 전멸")
    report = sl.advance_settlement(
        db, balance, rng, run_id=run_id,
        content_version_id=run["content_version_id"])
    return {"screen": "settlement", "report": report, "cleared": False}


def _post_battle_rewards(db: Database, balance: Balance, rng: JournaledRng, run,
                         battle) -> dict:
    """§15.4 — 탐험 자금 plus drop rolls. All drops go to the RUN INVENTORY
    (§8.6.2) and are subject to §15.10 at settlement."""
    run_id = run["run_id"]
    node_index = battle["node_index"]
    op_key = f"node:{node_index}:a{battle['attempt_no']}:rewards"

    encounter = db.one(
        "SELECT kind FROM encounters WHERE content_version_id = ? "
        "AND encounter_id = ?",
        (run["content_version_id"], battle["source_encounter_id"]))
    kind = encounter["kind"] if encounter else "normal"
    income = balance.get("run_currency_income")
    band = income["elite_clear"] if kind == "elite" else income["combat_clear"]
    currency = rng.randint(f"{op_key}:currency", int(band[0]), int(band[1]))

    rates = balance.get("drop_rates")
    drops: list[dict] = []
    with db.tx() as conn:
        conn.execute(
            "UPDATE runs SET run_currency = run_currency + ? WHERE run_id = ?",
            (currency, run_id))

        if rng.chance(f"{op_key}:equip", float(rates["equipment_combat"])):
            drops.append(_drop_equipment(db, rng, run, op_key))
        if rng.chance(f"{op_key}:stone", float(rates["stone_combat"])):
            low, high = rates["stone_combat_amount"]
            amount = rng.randint(f"{op_key}:stone:n", int(low), int(high))
            drops.append(_drop_stone(db, run, amount))

    # §20.2 engine hook — the boss ladder is advanced at settlement, but a
    # normal clear still counts enemies killed.
    killed = db.one(
        "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? "
        "AND side = 'enemy' AND is_alive = 0", (battle["battle_id"],))
    if killed and killed["n"]:
        ach.advance_counter(
            db, run["user_id"], ach.ENEMY_KILLED, int(killed["n"]),
            mutation_id=f"battle:{battle['battle_id']}:kills",
            content_version_id=run["content_version_id"])

    enc.sync_party_hp_to_run(db, battle["battle_id"], run_id)
    return {"run_currency": currency, "drops": drops}


def _drop_equipment(db: Database, rng: JournaledRng, run, op_key: str) -> dict:
    tier = sl.drop_tier_for(db, run["content_version_id"], run["world_id"],
                            int(run["deepest_depth_reached"]))
    defs = db.query(
        "SELECT equipment_def_id FROM equipment_defs WHERE content_version_id = ? "
        "ORDER BY equipment_def_id", (run["content_version_id"],))
    if not defs:
        return {"kind": "equipment", "skipped": "no equipment authored"}
    def_id = rng.choice(f"{op_key}:equip:pick",
                        [row["equipment_def_id"] for row in defs])
    db.execute(
        "INSERT INTO run_inventory (run_id, kind, equipment_def_id, tier, amount, "
        "acquired_at_depth, acquired_at) VALUES (?, 'equipment', ?, ?, 1, ?, ?)",
        (run["run_id"], def_id, tier, run["deepest_depth_reached"], utcnow()))
    return {"kind": "equipment", "equipment_def_id": def_id, "tier": tier}


def _drop_stone(db: Database, run, amount: int) -> dict:
    """Stones drop with the same band-and-depth rule, so low-tier stones stay
    obtainable for the escalating B(N) requirement (§8.4, §8.6.1)."""
    tier = max(1, sl.drop_tier_for(db, run["content_version_id"], run["world_id"],
                                   int(run["deepest_depth_reached"])))
    db.execute(
        "INSERT INTO run_inventory (run_id, kind, stone_tier, tier, amount, "
        "acquired_at_depth, acquired_at) VALUES (?, 'stone', ?, ?, ?, ?, ?)",
        (run["run_id"], tier, tier, amount, run["deepest_depth_reached"], utcnow()))
    return {"kind": "stone", "tier": tier, "amount": amount}
