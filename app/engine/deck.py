"""§2.2 draw/discard and §2.7.3 cursed-card insertion.

Decks are **per-character** — each has its own draw pile, discard pile, and run
deck. §2.10 restricts a character to cards matching its own element, so a
shared party deck would constantly deal unplayable cards.
"""

from __future__ import annotations

from app.db.connection import Database
from app.engine.rng import JournaledRng, key_curse_insert, key_shuffle

DRAW = "draw"
DISCARD = "discard"


def pile_size(db: Database, run_id: int, party_slot: int, pile: str) -> int:
    row = db.one(
        "SELECT COUNT(*) AS n FROM run_deck_cards "
        "WHERE run_id = ? AND party_slot = ? AND pile = ?",
        (run_id, party_slot, pile),
    )
    return int(row["n"])


def peek_draw(db: Database, run_id: int, party_slot: int, count: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT * FROM run_deck_cards WHERE run_id = ? AND party_slot = ? "
        "AND pile = ? ORDER BY pile_position LIMIT ?",
        (run_id, party_slot, DRAW, count),
    )]


def draw_cards(db: Database, run_id: int, party_slot: int, count: int) -> list[dict]:
    """Take up to `count` from the top of the draw pile.

    §2.2 partial draw: if fewer than `count` remain, draw what remains and play
    normally. The reshuffle happens at end of turn, not here.
    """
    cards = peek_draw(db, run_id, party_slot, count)
    for card in cards:
        db.execute(
            "UPDATE run_deck_cards SET pile = ?, pile_position = ? "
            "WHERE card_instance_id = ?",
            ("in_hand", -card["card_instance_id"], card["card_instance_id"]),
        )
    return cards


def discard_hand(db: Database, run_id: int, party_slot: int) -> int:
    """§2.11 step 7 — unselected cards are discarded at end of turn.

    No persistent hand exists, so everything drawn this turn moves together.
    """
    rows = db.query(
        "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
        "AND party_slot = ? AND pile = ? ORDER BY card_instance_id",
        (run_id, party_slot, "in_hand"),
    )
    position = _next_position(db, run_id, party_slot, DISCARD)
    for row in rows:
        db.execute(
            "UPDATE run_deck_cards SET pile = ?, pile_position = ? "
            "WHERE card_instance_id = ?",
            (DISCARD, position, row["card_instance_id"]),
        )
        position += 1
    return len(rows)


def _next_position(db: Database, run_id: int, party_slot: int, pile: str) -> int:
    row = db.one(
        "SELECT MAX(pile_position) AS m FROM run_deck_cards "
        "WHERE run_id = ? AND party_slot = ? AND pile = ?",
        (run_id, party_slot, pile),
    )
    return (row["m"] + 1) if row["m"] is not None else 0


def reshuffle(db: Database, rng: JournaledRng, run_id: int, party_slot: int,
              battle_id: int, round_no: int) -> int:
    """Move the discard pile back into the draw pile in a journaled order.

    A cursed card is **not consumed on use** (§2.7.1) — it goes to discard and
    returns on the next reshuffle, which is what makes it a recurring tax.
    """
    rows = db.query(
        "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
        "AND party_slot = ? AND pile = ? ORDER BY pile_position",
        (run_id, party_slot, DISCARD),
    )
    if not rows:
        return 0

    ids = [row["card_instance_id"] for row in rows]
    order = rng.shuffled(key_shuffle(battle_id, round_no, party_slot), ids)

    # Park the cards outside both piles first: the UNIQUE(run_id, party_slot,
    # pile, pile_position) index would otherwise collide with the draw pile's
    # existing positions mid-update.
    for card_id in ids:
        db.execute(
            "UPDATE run_deck_cards SET pile = ?, pile_position = ? "
            "WHERE card_instance_id = ?",
            ("reshuffling", -card_id, card_id),
        )
    position = _next_position(db, run_id, party_slot, DRAW)
    for card_id in order:
        db.execute(
            "UPDATE run_deck_cards SET pile = ?, pile_position = ? "
            "WHERE card_instance_id = ?",
            (DRAW, position, card_id),
        )
        position += 1
    return len(order)


def insert_cursed_card(db: Database, rng: JournaledRng, run_id: int,
                       party_slot: int, cursed_card_id: str, seq) -> int:
    """§2.7.3 — atomic insertion at a journaled random draw-pile position.

        n = COUNT(draw pile)
        k = rng_journaled_draw(op_key, 0 .. n)     # a replay reuses the SAME k
        shift every card at pile_position >= k up by one
        insert at pile_position = k

    `k` is an **ordinal into the draw pile**, not a raw `pile_position`: after
    cards have been drawn the surviving positions are sparse and no longer
    start at 0, so writing `k` as an absolute position would crowd every
    insertion into the top of the pile.

    UNIQUE(run_id, party_slot, pile, pile_position) makes a partially applied
    shift fail loudly instead of producing two cards at one index with
    SQL-order-dependent draw behavior.

    `seq` must be unique per insertion within the run — it is what makes the
    §16.4 op_key distinguish one curse from the next. A replay of the *same*
    insertion reuses its k; two different insertions must not share one.

    It cannot be drawn the same turn: the current turn's draw set is already
    fixed in `battle_draw`.
    """
    op_key = key_curse_insert(party_slot, seq)
    with db.tx() as conn:
        positions = [
            row["pile_position"]
            for row in conn.execute(
                "SELECT pile_position FROM run_deck_cards WHERE run_id = ? "
                "AND party_slot = ? AND pile = ? ORDER BY pile_position",
                (run_id, party_slot, DRAW),
            ).fetchall()
        ]
        n = len(positions)
        k = rng.randint(op_key, 0, n)

        # Ordinal k → the absolute position it must occupy. k == n appends
        # past the tail, which needs no shift at all.
        if k == n:
            target = (positions[-1] + 1) if positions else 0
        else:
            target = positions[k]
            # Shift downward from the tail so each intermediate step stays unique.
            for existing in conn.execute(
                "SELECT card_instance_id, pile_position FROM run_deck_cards "
                "WHERE run_id = ? AND party_slot = ? AND pile = ? "
                "AND pile_position >= ? ORDER BY pile_position DESC",
                (run_id, party_slot, DRAW, target),
            ).fetchall():
                conn.execute(
                    "UPDATE run_deck_cards SET pile_position = ? "
                    "WHERE card_instance_id = ?",
                    (existing["pile_position"] + 1, existing["card_instance_id"]),
                )

        cursor = conn.execute(
            "INSERT INTO run_deck_cards (run_id, party_slot, card_id, is_cursed, "
            "pile, pile_position) VALUES (?, ?, ?, 1, ?, ?)",
            (run_id, party_slot, cursed_card_id, DRAW, target),
        )
        return int(cursor.lastrowid)


def cursed_cards_in_deck(db: Database, run_id: int,
                         party_slot: int | None = None) -> list[dict]:
    sql = "SELECT * FROM run_deck_cards WHERE run_id = ? AND is_cursed = 1"
    params: list = [run_id]
    if party_slot is not None:
        sql += " AND party_slot = ?"
        params.append(party_slot)
    sql += " ORDER BY card_id, card_instance_id"
    return [dict(row) for row in db.query(sql, tuple(params))]


def remove_card(db: Database, card_instance_id: int) -> None:
    """Physically remove a card from the run deck (cleanse, deck thinning).

    Positions are left sparse on purpose — the draw order only ever reads them
    with ORDER BY, and re-packing would invite the very index collisions
    §2.7.3's UNIQUE constraint exists to catch.
    """
    db.execute("DELETE FROM run_deck_cards WHERE card_instance_id = ?",
               (card_instance_id,))


def add_card(db: Database, run_id: int, party_slot: int, card_id: str,
             pile: str = DISCARD) -> int:
    position = _next_position(db, run_id, party_slot, pile)
    cursor = db.execute(
        "INSERT INTO run_deck_cards (run_id, party_slot, card_id, is_cursed, "
        "pile, pile_position) VALUES (?, ?, ?, 0, ?, ?)",
        (run_id, party_slot, card_id, pile, position),
    )
    return int(cursor.lastrowid)
