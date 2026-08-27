"""게임 상태를 화면 그림으로 바꾸는 곳.

핸들러는 규칙을 다루고, `panels` 는 그리기만 안다. 그 사이에서 "이 화면을
그리려면 무엇을 조회해야 하는가"를 아는 것이 여기다.

여기서 나온 첨부는 응답 dict 의 `attachments` 에 실려 중앙봇으로 나간다.
그림을 만들지 못해도 화면은 나가야 하므로(§11), 모든 함수가 실패를 삼키고
빈 목록을 돌려준다 — 글자만 있는 화면이 화면이 없는 것보다 낫다.
"""

from __future__ import annotations

import json
import logging

from app.db.connection import Database
from app.engine import statuses as st
from app.engine import timed_effects as te
from app.engine import units as un
from app.render import panels
from app.render import theme as theme_module

logger = logging.getLogger(__name__)


def attach(*attachments) -> list[dict]:
    """중앙봇이 받는 모양으로 바꾼다 (§1.3.7).

    `content_type` 이 빠져 있었다 — 첨부가 전부 PNG 인데도 그 사실을 응답에
    적어 보내지 않고 있었다."""
    return [{"filename": item.filename, "data_b64": item.data_b64,
             "content_type": "image/png"}
            for item in attachments if item is not None]


def safely(build) -> list[dict]:
    """그림을 만들다 실패해도 화면은 나간다 (§11).

    렌더는 곁가지다. 여기서 터진 예외가 플레이어의 조작을 통째로 되돌리면
    이벤트가 기록되지 않은 채 중앙봇이 무한히 재전송하게 된다 (§16.7).
    """
    try:
        return build()
    except Exception:                                    # noqa: BLE001
        logger.exception("화면 렌더에 실패해 글자만 내보냅니다")
        return []


# =====================================================================
# 전투
# =====================================================================
def battle(db: Database, balance, *, battle_id: int, run) -> list[dict]:
    """아군·적 패널과 손패 (§11, §1.3.7)."""

    def build() -> list[dict]:
        from app.engine import battle as bt

        row = db.one("SELECT * FROM battles WHERE battle_id = ?", (battle_id,))
        if row is None:
            return []

        allies = [_unit_view(db, unit, run) for unit in
                  un.load_units(db, battle_id, side=un.ALLY)]
        enemies = [_unit_view(db, unit, run) for unit in
                   un.load_units(db, battle_id, side=un.ENEMY)]

        engine = bt.build_engine(
            db, balance, battle_id=battle_id, run_id=run["run_id"],
            content_version_id=run["content_version_id"], rng=None)
        telegraphs = {entry["enemy_unit_id"]: entry
                      for entry in engine.telegraphs()}

        from app.engine import passives as pv

        return attach(*panels.render_battle_screen(
            allies, enemies, telegraphs,
            resource=int(row["party_resource_current"] or 0),
            round_no=int(row["round_no"] or 1),
            hand=_hand(db, run),
            passives=pv.equipped(db, battle_id, run["content_version_id"]),
            log=bt.recent_log(db, battle_id,
                              theme_module.load().int_("battle_log_lines"))))

    return safely(build)


def _unit_view(db: Database, unit: un.Unit, run) -> dict:
    """`panels` 가 읽는 모양으로. 이름은 콘텐츠에서 가져온다."""
    is_ally = unit.side == un.ALLY
    if is_ally:
        row = db.one(
            "SELECT name, base_rarity AS rank FROM characters "
            "WHERE content_version_id = ? AND character_id = ?",
            (run["content_version_id"], unit.unit_def_id))
        tier = int(row["rank"]) if row else 1
    else:
        row = db.one(
            "SELECT name, tier FROM enemies "
            "WHERE content_version_id = ? AND enemy_id = ?",
            (run["content_version_id"], unit.unit_def_id))
        # 적의 tier 는 '일반 | 엘리트 | 보스' 라서 희귀도 색 번호로 바꿔 준다.
        tier = {"일반": 2, "엘리트": 4, "보스": 6}.get(
            row["tier"] if row else "", 1)

    view = {
        "battle_unit_id": unit.battle_unit_id,
        "name": row["name"] if row else unit.unit_def_id,
        "tier": tier,
        # 대상 선택 드롭다운(§11)이 이 번호로 대상을 고른다 — 그림 위 번호
        # 배지가 이걸 그대로 써야 "몇 번"이 그림과 드롭다운에서 같은 뜻이
        # 된다. 그 전에는 그림이 목록 순서를 다시 세어(1,2,3…) 매겨서, 죽은
        # 유닛이 섞이면 드롭다운의 "슬롯 3"과 그림의 "③"이 서로 다른
        # 유닛일 수 있었다.
        "visible_slot": unit.visible_slot,
        "hp_current": unit.hp_current,
        "hp_max": unit.hp_max,
        "block": unit.block,
        "is_alive": unit.is_alive,
        "statuses": st.active_statuses(db, unit.battle_unit_id),
        # 무적·라운드 한정 스탯 변화는 전투 계산에는 이미 반영되지만(§2.5.3),
        # 화면에는 지금까지 하나도 나오지 않았다 — 왜 대미지가 0인지,
        # 왜 공격력이 갑자기 달라졌는지 플레이어가 알 방법이 없었다.
        "timed_effects": te.active_for_unit(db, unit.battle_unit_id),
    }
    view["character_id" if is_ally else "enemy_id"] = unit.unit_def_id
    return view


def _hand(db: Database, run) -> list[dict]:
    """지금 낼 수 있는 카드들. 업그레이드가 반영된 모습으로 보여준다."""
    from app.engine import card_upgrades as cu

    snapshot = db.one(
        "SELECT card_upgrade_json FROM run_build_snapshot WHERE run_id = ? "
        "ORDER BY party_slot LIMIT 1", (run["run_id"],))
    tiers = json.loads(snapshot["card_upgrade_json"]) if snapshot else {}

    hand = []
    for index, row in enumerate(db.query(
        "SELECT rdc.card_id, rdc.is_cursed, c.name, c.cost, c.element, "
        "c.rarity_tier, c.category, c.effects_json FROM run_deck_cards rdc "
        "JOIN cards c ON c.card_id = rdc.card_id AND c.content_version_id = ? "
        "WHERE rdc.run_id = ? AND rdc.pile = 'in_hand' ORDER BY rdc.pile_position",
        (run["content_version_id"], run["run_id"]),
    ), start=1):
        tier = int(tiers.get(row["card_id"], 0))
        card = cu.effective_card(db, run["content_version_id"], row, tier)
        hand.append({
            "card_id": row["card_id"], "name": row["name"],
            "cost": card["cost"], "element": row["element"],
            "rarity_tier": row["rarity_tier"], "category": row["category"],
            "upgrade_tier": tier,
            # 손패 드롭다운(§11)이 매기는 것과 같은 번호 — playable_cards()
            # 도 이 카드의 pile_position 을 똑같이 세어 매긴다.
            "hand_index": index,
        })
    return hand


# =====================================================================
# 지도
# =====================================================================
def game_map(db: Database, run, *, available: set[int] | None = None) -> list[dict]:
    """지도 한 장.

    `available` 을 넘기지 않으면 지금 갈 수 있는 칸을 직접 구한다. 예전에는
    아무도 넘기지 않아 **모든 칸이 "지나온 칸" 색으로 그려졌다** — 버튼은
    갈 곳을 알려 주는데 그림은 지도 전체가 이미 끝난 것처럼 보였다.
    """

    def build() -> list[dict]:
        from app.engine import map_gen

        nodes = [dict(row) for row in db.query(
            "SELECT node_index, depth, node_type, state FROM run_nodes "
            "WHERE run_id = ? ORDER BY node_index", (run["run_id"],))]
        if not nodes:
            return []
        edges = [(row["from_node_index"], row["to_node_index"]) for row in db.query(
            "SELECT from_node_index, to_node_index FROM run_edges WHERE run_id = ?",
            (run["run_id"],))]
        options = available
        if options is None:
            options = {entry["node_index"] for entry in
                       map_gen.available_next_nodes(db, run["run_id"],
                                                    run["current_node_index"])}
        visited = {node["node_index"] for node in nodes
                   if node.get("state") == map_gen.NODE_VISITED}
        return attach(panels.render_map(
            nodes, edges, current_node_index=run["current_node_index"],
            available=options, visited=visited, world_id=run["world_id"]))

    return safely(build)


# =====================================================================
# 상점
# =====================================================================
def shop(db: Database, run, items: list[dict]) -> list[dict]:
    def build() -> list[dict]:
        rendered = []
        for item in items:
            reference = item.get("item_ref")
            entry = json.loads(reference) if isinstance(reference, str) else dict(item)
            entry["price"] = item.get("price", entry.get("price", 0))
            entry["purchased"] = item.get("purchased", 0)
            if entry.get("kind") == "card":
                card = db.one(
                    "SELECT cost, rarity_tier, category FROM cards "
                    "WHERE content_version_id = ? AND card_id = ?",
                    (run["content_version_id"], entry.get("card_id")))
                if card:
                    entry.update({"cost": card["cost"],
                                  "rarity_tier": card["rarity_tier"],
                                  "category": card["category"]})
            rendered.append(entry)
        return attach(panels.render_shop(
            rendered, currency=int(run["run_currency"] or 0)))

    return safely(build)


# =====================================================================
# 뽑기
# =====================================================================
def banner(db: Database, balance, row, *, user_id: int, carta: int) -> list[dict]:
    def build() -> list[dict]:
        rates = balance.get("gacha_base_rates")
        pity = db.one(
            "SELECT pull_count, guarantee_pending FROM gacha_pity "
            "WHERE user_id = ? AND pity_scope_id = ?",
            (user_id, row["pity_scope_id"] if "pity_scope_id" in row.keys()
             else "standard_global"))
        hard = int(balance.get("pity_hard"))
        pulled = int(pity["pull_count"]) if pity else 0
        entry = dict(row)
        # 배너에는 이름 열이 없다 — id 를 그대로 보여준다.
        entry.setdefault("name", entry.get("banner_id", ""))
        return attach(panels.render_banner(
            entry,
            rates={"최고": rates["top"], "중간": rates["mid"], "기본": rates["base"]},
            pity={"until_hard": max(0, hard - pulled),
                  "guarantee_pending": bool(pity["guarantee_pending"]) if pity
                  else False},
            carta=carta))

    return safely(build)


def gacha_results(db: Database, content_version_id: int,
                  results: list[dict]) -> list[dict]:
    def build() -> list[dict]:
        rendered = []
        for result in results:
            entry = dict(result)
            if entry.get("kind") == "character":
                row = db.one(
                    "SELECT name, base_rarity FROM characters "
                    "WHERE content_version_id = ? AND character_id = ?",
                    (content_version_id, entry.get("entity_id")))
                if row:
                    entry.setdefault("name", row["name"])
                    entry.setdefault("star_rank", row["base_rarity"])
            else:
                row = db.one(
                    "SELECT name, rarity_tier FROM cards "
                    "WHERE content_version_id = ? AND card_id = ?",
                    (content_version_id, entry.get("entity_id")))
                if row:
                    entry.setdefault("name", row["name"])
                    entry.setdefault("rarity_tier", row["rarity_tier"])
            rendered.append(entry)
        return attach(panels.render_gacha_results(rendered))

    return safely(build)


# =====================================================================
# 준비 화면
# =====================================================================
def prep(db: Database, balance, *, user_id: int, content_version_id: int,
         world_name: str, party: list[dict],
         passives: list[dict] | None = None) -> list[dict]:
    """확정 화면 — 여기 보이는 값이 런 시작 시점에 얼려지는 값이다 (§16.2.3)."""

    def build() -> list[dict]:
        return attach(panels.render_prep(party, world=world_name,
                                         passives=passives))

    return safely(build)


def deck(db: Database, balance, *, user_id: int, content_version_id: int,
         character_id: str, chosen: list[str]) -> list[dict]:
    """덱 구성 화면 — 지금 고른 대로라면 덱이 어떻게 되는지 그대로 보여준다.

    화면과 실제로 만들어질 덱이 다르면 안 되므로, 구성 규칙은 엔진과 같은
    함수(`lifecycle.preview_deck`)에서 가져온다.
    """

    def build() -> list[dict]:
        from app.content import catalog
        from app.engine import lifecycle as lc

        character = catalog.find(db, content_version_id, character_id,
                                 user_id=user_id)
        composed = lc.preview_deck(db, balance, user_id, character_id,
                                   content_version_id, chosen=chosen)
        counts: dict[str, int] = {}
        for card_id in composed:
            counts[card_id] = counts.get(card_id, 0) + 1

        cards = []
        for card_id, count in counts.items():
            entry = catalog.find(db, content_version_id, card_id, user_id=user_id)
            if entry is None:
                continue
            art = entry.as_art()
            art["count"] = count
            cards.append(art)

        return attach(panels.render_deck(
            cards, character=character.as_art() if character else {},
            title=f"{character.name if character else character_id}의 덱",
            total=len(composed)))

    return safely(build)


def passive_collection(db: Database, *, user_id: int,
                       content_version_id: int) -> list[dict]:
    """보유 패시브 한 장 (§6)."""

    def build() -> list[dict]:
        from app.engine import passives as pv

        owned = pv.owned(db, user_id, content_version_id)
        if not owned:
            return []
        return attach(panels.render_collection(
            [{"card_id": row["passive_card_id"], "name": row["name"],
              "rarity_tier": row["rarity_tier"], "kind": "passive"}
             for row in owned],
            title=f"패시브 {len(owned)}장"))

    return safely(build)


def collection(db: Database, *, user_id: int,
               content_version_id: int) -> list[dict]:
    """소장 카드 한 장 — 캐릭터 카드와 행동 카드를 한 화면에 (§5)."""

    def build() -> list[dict]:
        from app.content import catalog

        cards = catalog.owned(db, user_id, content_version_id)
        if not cards:
            return []
        return attach(panels.render_collection(
            [card.as_art() | {"kind": card.kind} for card in cards],
            title=f"소장 {len(cards)}장"))

    return safely(build)


def compendium(db: Database, *, user_id: int,
               content_version_id: int) -> list[dict]:
    """§5.3a 카드 도감 — 안 가진 칸도 가려서 그대로 보여준다."""

    def build() -> list[dict]:
        from app.content import catalog

        cards = catalog.compendium(db, user_id, content_version_id)
        if not cards:
            return []
        owned_count = sum(1 for card in cards if card.owned)
        return attach(panels.render_compendium(
            [card.as_art() | {"owned": card.owned} for card in cards],
            title=f"카드 도감 {owned_count}/{len(cards)}"))

    return safely(build)


# =====================================================================
# 가입 (A-1.1)
# =====================================================================
def join_promo() -> list[dict]:
    """가입 화면의 고정 홍보 그림. Pillow로 그리지 않는다 — 있으면 그대로
    붙이고, 없으면(아직 콘텐츠 저작 전) 첨부 없이 화면이 나간다."""

    def build() -> list[dict]:
        from app.render.assets import AssetLibrary

        library = AssetLibrary(theme_module.load())
        image = library.load("promo", "join")
        if image is None:
            return []
        return attach(panels.to_attachment(image, "deckout_join.png"))

    return safely(build)


# =====================================================================
# 허브 — 요약, 캐릭터, 장비, 연구, 업적
# =====================================================================
def hub(dashboard: dict, *, coin: int | None, daily: dict,
       note: str | None = None, avatar_image=None) -> list[dict]:
    return safely(lambda: attach(panels.render_hub(
        dashboard, coin=coin, daily=daily, note=note, avatar_image=avatar_image)))


def characters(rows: list[dict]) -> list[dict]:
    return safely(lambda: attach(panels.render_characters(rows)) if rows else [])


def equipment(rows: list[dict], *, stones: list[dict] | None = None) -> list[dict]:
    return safely(lambda: attach(panels.render_equipment(rows, stones=stones))
                 if rows else [])


def hub_shop(equipment_listing: list[dict], stones: list[dict], *,
            currency: int | None) -> list[dict]:
    return safely(lambda: attach(panels.render_hub_shop(
        equipment_listing, stones, currency=currency)))


def research(listing: list[dict]) -> list[dict]:
    return safely(lambda: attach(panels.render_research(listing)) if listing else [])


def achievements(listing: list[dict]) -> list[dict]:
    return safely(lambda: attach(panels.render_achievements(listing))
                 if listing else [])


def run_deck(rows: list[dict]) -> list[dict]:
    return safely(lambda: attach(panels.render_run_deck(rows)) if rows else [])


# =====================================================================
# 정산
# =====================================================================
def settlement(report: dict) -> list[dict]:
    return safely(lambda: attach(panels.render_settlement(report)))
