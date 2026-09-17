"""Discord skill-count editor. Paging keeps every unlocked skill reachable."""
from collections import Counter

from app.api import visuals
from app.content.balance import Balance
from app.engine import lifecycle as lc, loadouts

PREFIX = "dko:prep:"


def screen(db, user_id, version, draft, *, slot, page=0):
    from app.api import screens
    character_id = draft["party"][slot - 1]
    loadouts.backfill(db, user_id, version)
    balance = Balance(db, version)
    slots = loadouts.skill_slots(balance)
    note = ""
    if slot not in draft["deck"]:
        picked = loadouts.saved(db, balance, user_id, version, character_id)
        if picked is None:
            picked = lc.preview_deck(db, balance, user_id, character_id, version)[-slots:]
            picked = [c for c in picked if c not in loadouts.BASICS]
            note = "자동 편성 초안입니다. 카드별 장수를 바꾸거나 그대로 저장하세요."
        else:
            note = "이 캐릭터의 저장된 덱을 불러왔습니다."
        draft["deck"][slot] = picked
        screens.save_draft(db, user_id, draft)
    cards = sorted(loadouts.legal_skills(db, user_id, version, character_id),
                   key=lambda c: (c.category, c.cost, c.card_id))
    count = Counter(draft["deck"][slot])
    pages = max(1, (len(cards) + 24) // 25)
    page = max(0, min(page, pages - 1))
    character = db.one("SELECT name FROM characters WHERE content_version_id=? AND character_id=?",
                       (version, character_id))
    comp = balance.get("starter_deck_composition")
    lines = [f"**준비 화면** — [3] 덱 구성 ({slot}/{len(draft['party'])})",
             character["name"], screens.boss_hint(db, user_id, version, draft),
             f"기본 공격 {comp['평타']}장 · 기본 방어 {comp['기본_방어']}장 고정",
             f"스킬 {sum(count.values())}/{slots}장 · 같은 스킬 최대 {balance.get('deck_skill_copy_limit', slots)}장",
             "한 번 해금하면 여러 장 편성할 수 있습니다. 중복 획득은 강화 재료로 사용합니다."]
    if note:
        lines.append(note)
    lines.extend(f"• {c.name}: {count[c.card_id]}장" for c in cards if count[c.card_id])
    components = []
    if cards:
        components.append({
            "type": "string_select", "custom_id": f"{PREFIX}deck_pick:{slot}",
            "placeholder": f"장수를 바꿀 스킬 ({page + 1}/{pages})",
            "min_values": 1, "max_values": 1,
            "options": [{"label": f"{c.name} · {count[c.card_id]}장"[:100],
                         "description": f"비용 {c.cost} · {c.category} · 강화 +{c.upgrade_tier}",
                         "value": c.card_id} for c in cards[page * 25:(page + 1) * 25]]
        })
    components += [
        {"type": "button", "custom_id": f"{PREFIX}deck_save:{slot}", "label": "저장하고 다음",
         "disabled": sum(count.values()) != slots},
        {"type": "button", "custom_id": f"{PREFIX}deck_auto:{slot}", "label": "자동 편성 저장"},
        {"type": "button", "custom_id": f"{PREFIX}deck_reset:{slot}", "label": "스킬 비우기"},
    ]
    if page > 0:
        components.append({"type": "button", "custom_id": f"{PREFIX}deck_page:{slot}:{page-1}", "label": "이전 목록"})
    if page + 1 < pages:
        components.append({"type": "button", "custom_id": f"{PREFIX}deck_page:{slot}:{page+1}", "label": "다음 목록"})
    # Put the complete-deck action first; selection remains available below.
    components.sort(key=lambda c: 0 if "deck_save:" in c["custom_id"] else
                    1 if "deck_auto:" in c["custom_id"] else 2)
    return {"action": "edit", "content": "\n".join(lines), "components": components,
            "attachments": visuals.deck(db, balance, user_id=user_id, content_version_id=version,
                                        character_id=character_id, chosen=draft["deck"][slot])}


def handle(db, balance, user_id, version, draft, step, values):
    from app.api import screens, errors
    invalid = {"action": "edit", "content": errors.ILLEGAL_STATE}
    parts = step.split(":")
    action = parts[0]
    try:
        slot = int(parts[1])
        if not 1 <= slot <= len(draft["party"]):
            return invalid
        character_id = draft["party"][slot - 1]
        cards = list(draft["deck"].get(slot, []))
        legal = {c.card_id: c for c in loadouts.legal_skills(db, user_id, version, character_id)}
        if action == "deck_page":
            return screen(db, user_id, version, draft, slot=slot, page=int(parts[2]))
        if action == "deck_pick":
            if len(values) != 1 or values[0] not in legal:
                return invalid
            card_id = values[0]
            # Set an absolute count, so retries cannot add another copy.
            cap = min(int(balance.get("deck_skill_copy_limit", loadouts.skill_slots(balance))),
                      loadouts.skill_slots(balance) - len(cards) + cards.count(card_id))
            return {"action": "edit",
                    "content": f"**{legal[card_id].name}** — 현재 {cards.count(card_id)}장\n"
                               "장수를 선택하세요. 다른 스킬을 줄이면 더 넣을 수 있습니다.",
                    "components": [
                        {"type": "string_select", "custom_id": f"{PREFIX}deck_count:{slot}:{card_id}",
                         "placeholder": "이 스킬의 장수", "min_values": 1, "max_values": 1,
                         "options": [{"label": f"{n}장", "value": str(n)} for n in range(cap + 1)]},
                        {"type": "button", "custom_id": f"{PREFIX}deck_page:{slot}:0", "label": "덱으로 돌아가기"}]}
        if action == "deck_count":
            card_id = parts[2]
            if card_id not in legal or len(values) != 1:
                return invalid
            n = int(values[0])
            if not 0 <= n <= loadouts.skill_slots(balance):
                return invalid
            cards = [c for c in cards if c != card_id] + [card_id] * n
            loadouts.validate(db, balance, user_id, version, character_id, cards, complete=False)
        elif action == "deck_reset":
            cards = []
        elif action == "deck_auto":
            cards = lc.preview_deck(db, balance, user_id, character_id, version)[-loadouts.skill_slots(balance):]
        elif action == "deck":
            # Older messages may still submit the previous select control.
            # Accept only a complete exact deck; never silently repeat choices.
            cards = list(values)
            if any(c not in legal for c in cards):
                return invalid
        elif action != "deck_save":
            return invalid
        if action in ("deck_save", "deck_auto", "deck"):
            loadouts.save(db, balance, user_id, version, character_id, cards)
        draft["deck"][slot] = cards
        screens.save_draft(db, user_id, draft)
        if action in ("deck_count", "deck_reset"):
            return screen(db, user_id, version, draft, slot=slot)
        if slot < len(draft["party"]):
            return screen(db, user_id, version, draft, slot=slot + 1)
        return screens.passive_select_screen(db, user_id, version, draft)
    except (ValueError, IndexError) as error:
        return {"action": "edit", "content": str(error),
                "components": [{"type": "button", "custom_id": f"{PREFIX}deck_page:{slot}:0",
                                "label": "덱으로 돌아가기"}]} if "slot" in locals() else invalid
