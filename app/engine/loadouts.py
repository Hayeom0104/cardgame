"""Exact skill counts, one persistent deck per character, and starter grants."""
from __future__ import annotations

import json
from collections import Counter

from app.content import catalog
from app.content.balance import Balance
from app.content.seed import CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE
from app.db.connection import utcnow

BASICS = (CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE)


def skill_slots(balance):
    composition = balance.get("starter_deck_composition")
    return int(balance.get("base_deck_size")) - int(composition["평타"]) - int(composition["기본_방어"])


def legal_skills(db, user_id, version, character_id):
    return [card for card in catalog.playable_for(db, user_id, version, character_id)
            if card.card_id not in BASICS]


def validate(db, balance, user_id, version, character_id, cards, *, complete=True):
    if not db.one("SELECT 1 FROM owned_characters WHERE user_id=? AND character_id=?",
                  (user_id, character_id)):
        raise ValueError("보유하지 않은 캐릭터입니다.")
    legal = {card.card_id for card in legal_skills(db, user_id, version, character_id)}
    if not isinstance(cards, list) or any(not isinstance(c, str) or c not in legal for c in cards):
        raise ValueError("낼 수 없거나 해금하지 않은 스킬입니다.")
    slots = skill_slots(balance)
    if len(cards) > slots or (complete and len(cards) != slots):
        raise ValueError(f"스킬을 정확히 {slots}장 편성해 주세요. 현재 {len(cards)}장입니다.")
    cap = int(balance.get("deck_skill_copy_limit", slots))
    if any(count > cap for count in Counter(cards).values()):
        raise ValueError(f"같은 스킬은 최대 {cap}장까지 편성할 수 있습니다.")


def saved(db, balance, user_id, version, character_id):
    row = db.one("SELECT cards_json FROM character_decks WHERE user_id=? AND character_id=?",
                 (user_id, character_id))
    if not row:
        return None
    try:
        cards = json.loads(row["cards_json"])
        validate(db, balance, user_id, version, character_id, cards)
        return cards
    except (ValueError, TypeError):
        # Retired or changed cards invalidate the saved deck; the editor explains
        # its automatic fallback and never changes an already running deck.
        return None


def save(db, balance, user_id, version, character_id, cards):
    validate(db, balance, user_id, version, character_id, cards)
    db.execute("INSERT INTO character_decks(user_id,character_id,cards_json,updated_at) "
               "VALUES(?,?,?,?) ON CONFLICT(user_id,character_id) DO UPDATE SET "
               "cards_json=excluded.cards_json,updated_at=excluded.updated_at",
               (user_id, character_id, json.dumps(cards, ensure_ascii=False), utcnow()))


def grant_starter_skills(conn, user_id, version, character_id):
    """Idempotent grant: existing tiers/materials are never touched."""
    row = conn.execute("SELECT value_json FROM balancing_constants WHERE "
                       "content_version_id=? AND key='character_starter_skills'", (version,)).fetchone()
    bundles = json.loads(row["value_json"]) if row else {}
    character = conn.execute("SELECT element FROM characters WHERE content_version_id=? "
                             "AND character_id=?", (version, character_id)).fetchone()
    if not character:
        return []
    granted = []
    for card_id in bundles.get(character_id, []):
        card = conn.execute("SELECT element,is_retired FROM cards WHERE "
                            "content_version_id=? AND card_id=?", (version, card_id)).fetchone()
        if not card or card["is_retired"] or card["element"] not in ("무속성", character["element"]):
            continue
        cursor = conn.execute("INSERT OR IGNORE INTO unlocked_cards "
                              "(user_id,card_id,upgrade_tier,unlocked_at) VALUES(?,?,0,?)",
                              (user_id, card_id, utcnow()))
        if cursor.rowcount:
            granted.append(card_id)
    return granted


def backfill(db, user_id, version):
    with db.tx() as conn:
        for row in conn.execute("SELECT character_id FROM owned_characters WHERE user_id=?",
                                (user_id,)).fetchall():
            grant_starter_skills(conn, user_id, version, row["character_id"])
