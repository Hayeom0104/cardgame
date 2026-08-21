"""런 화면의 버튼과 선택지 — 즉, 플레이어가 실제로 누를 수 있는 것.

## 왜 이 파일이 따로 있는가

`custom_id` 열 개 중 **여덟 개는 만들어지는 곳이 없었다.** 지도의 칸 버튼도,
전투의 카드 선택도, 보상 수령도, 상점 구매도, 이벤트 분기도 전부 처리하는
핸들러는 있는데 그것을 부를 컴포넌트가 어디에서도 그려지지 않았다. 스레드에는
그림과 글자만 나가고 누를 것이 없었다는 뜻이다.

테스트가 통과하고 있던 이유는 핸들러를 합성한 `custom_id` 로 직접 부르고
있어서다 — 화면이 그 `custom_id` 를 실제로 내놓는지는 아무도 확인하지 않았다.

## 세대와 리비전

`custom_id` 에는 `surface_generation` 과 `presentation_revision` 이 박혀 있고,
§1.3.10의 게이트 3·4가 그것을 런의 현재 값과 대조한다. 조작 하나가 §16.7 CAS로
리비전을 올리므로, **그 조작의 응답에 붙는 컴포넌트는 올라간 뒤의 값을 들고
있어야 한다.** 그래서 여기서는 언제나 런 행을 다시 읽는다 — 넘겨받은 값을
쓰면 방금 그린 버튼이 처음부터 만료된 상태가 된다.

## 그림과 같은 취급

`visuals` 가 화면별로 그림을 고르듯, 여기서는 화면별로 컨트롤을 고른다.
다만 그림과 달리 실패를 삼키지 않는다: 그림이 없는 화면은 볼 수 있지만
버튼이 없는 화면은 아무것도 할 수 없다.
"""

from __future__ import annotations

from app.api import custom_id as cid
from app.api import errors
from app.db.connection import Database

#: Discord 한 메시지의 컴포넌트 한도에 맞춘 상한.
MAX_OPTIONS = 25
MAX_BUTTONS = 5


def _surface(db: Database, run_id: int) -> tuple[int, int]:
    """지금 이 순간의 (세대, 리비전). 반드시 다시 읽는다."""
    row = db.one("SELECT surface_generation, presentation_revision FROM runs "
                 "WHERE run_id = ?", (run_id,))
    if row is None:
        raise KeyError(f"run {run_id} does not exist")
    return int(row["surface_generation"]), int(row["presentation_revision"])


def for_screen(db: Database, run_id: int, result: dict, *,
               engine=None) -> list[dict]:
    """이 화면에서 플레이어가 할 수 있는 것들.

    `result` 는 노드 해결이나 전투 진행이 돌려준 딕셔너리다. 알 수 없는
    화면이면 빈 목록 — 컨트롤이 없는 화면(정산 등)도 있다.
    """
    screen = result.get("screen")
    if screen == "map":
        return game_map(db, run_id)
    if screen == "reward":
        return reward(db, run_id, result.get("options") or [],
                      skip_available=bool(result.get("skip_available", True)))
    if screen == "shop":
        return shop(db, run_id, result.get("items") or [])
    if screen == "event":
        return event(db, run_id, result.get("options") or {})
    if screen == "battle" and engine is not None:
        return battle(db, run_id, engine)
    return []


# =====================================================================
# 지도 (§3.5)
# =====================================================================
def game_map(db: Database, run_id: int) -> list[dict]:
    """갈 수 있는 다음 칸 버튼. 이것이 없으면 런이 한 발짝도 못 나간다."""
    from app.engine import map_gen

    run = db.one("SELECT current_node_index FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        return []
    options = map_gen.available_next_nodes(db, run_id, run["current_node_index"])
    if not options:
        return []
    generation, revision = _surface(db, run_id)
    return [{
        "type": "button",
        "custom_id": cid.build(cid.ACTION_NODE_CHOOSE, run_id, generation,
                               revision, str(node["node_index"])),
        "label": f"{node['node_type']} (깊이 {node['depth']})"[:80],
    } for node in options[:MAX_BUTTONS]]


# =====================================================================
# 전투 (§2.11)
# =====================================================================
def battle(db: Database, run_id: int, engine) -> list[dict]:
    """지금 턴을 잡은 아군이 낼 수 있는 카드.

    §2.10과 자원과 침묵으로 이미 걸러진 목록을 그대로 쓴다 — 화면이 낼 수
    없는 카드를 내놓으면 눌러도 거절되는 선택지가 된다.
    """
    from app.engine import units as un

    unit = engine.acting_unit()
    if unit is None or unit.side != un.ALLY:
        return []
    playable = engine.playable_cards(unit)
    if not playable:
        return []

    generation, revision = _surface(db, run_id)
    return [{
        "type": "string_select",
        "custom_id": cid.build(cid.ACTION_CARD_SELECT, run_id, generation,
                               revision),
        "placeholder": "낼 카드를 고르세요",
        # 번호는 손패 그림(§11)의 카드 왼쪽 위 번호표와 같은 hand_index다 —
        # 그림에서 "3번 카드"를 봤다면 여기서도 "3."을 찾으면 된다.
        "options": [{
            "label": f"{entry['hand_index']}. {entry['card']['name']} "
                     f"({entry['card']['cost']})"[:100],
            "description": f"{entry['card']['element']} · "
                           f"{entry['card']['category']}"[:100],
            "value": str(entry["card_instance_id"]),
        } for entry in playable[:MAX_OPTIONS]],
    }]


# =====================================================================
# 보상 (§3.2)
# =====================================================================
def reward(db: Database, run_id: int, options: list[dict], *,
           skip_available: bool = True) -> list[dict]:
    """카드 하나와 그것을 받을 자리를 한 번에 고른다.

    덱은 캐릭터별이라 받는 자리가 반드시 필요하고(§3.2), 핸들러는 값을
    `카드id|자리` 형태로 읽는다. 그래서 후보 × 낼 수 있는 자리를 펼쳐 놓는다 —
    두 단계로 나누면 중간 상태를 저장해야 하고, 그 사이에 리비전이 움직인다.
    """
    if not options:
        return []
    generation, revision = _surface(db, run_id)

    entries = []
    for option in options:
        for slot in option.get("recipients") or []:
            entries.append({
                "label": f"{option['name']} → 자리 {slot}"[:100],
                "description": f"{option.get('element', '')} · "
                               f"★{option.get('rarity_tier', 1)}"[:100],
                "value": f"{option['card_id']}|{slot}",
            })
    if not entries:
        return []

    components: list[dict] = [{
        "type": "string_select",
        "custom_id": cid.build(cid.ACTION_REWARD_PICK, run_id, generation,
                               revision),
        "placeholder": "받을 카드를 고르세요",
        "options": entries[:MAX_OPTIONS],
    }]
    if skip_available:
        # §3.2 — "안 받기"는 선택이 아니라 의무다. 이것이 없으면 덱은 늘기만
        # 하고 덱을 얇게 유지하는 전략이 아예 사라진다.
        components.append({
            "type": "button",
            "custom_id": cid.build(cid.ACTION_SKIP, run_id, generation, revision),
            "label": errors.LABEL_SKIP,
        })
    return components


# =====================================================================
# 상점 (§7.1)
# =====================================================================
def shop(db: Database, run_id: int, items: list[dict]) -> list[dict]:
    """살 것 하나와 나가기. 나가기가 없으면 상점 칸에서 못 나온다.

    살 수 없는 것은 목록에서 뺀다. 탐험 자금은 **로컬 재화** 라 여기서 판단해도
    틀릴 일이 없다 — 코인과 달리 다른 미니게임이 중간에 가져갈 수 없으므로,
    §17.3이 코인 잔액으로 버튼을 막지 말라고 한 이유가 여기엔 없다. 걸러 내지
    않으면 빈털터리 플레이어에게 눌러도 늘 거절되는 목록만 남는다.
    """
    generation, revision = _surface(db, run_id)
    components: list[dict] = []

    run = db.one("SELECT run_currency FROM runs WHERE run_id = ?", (run_id,))
    purse = int(run["run_currency"]) if run else 0

    available = [item for item in items
                 if not item.get("purchased")
                 and int(item.get("price", 0)) <= purse]
    if available:
        components.append({
            "type": "string_select",
            "custom_id": cid.build(cid.ACTION_SHOP_BUY, run_id, generation,
                                   revision),
            "placeholder": "구매",
            "options": [{
                "label": _shop_label(item)[:100],
                "description": f"실버 {item.get('price', 0)}"[:100],
                "value": str(item["item_index"]),
            } for item in available[:MAX_OPTIONS]],
        })
    components.append({
        "type": "button",
        "custom_id": cid.build(cid.ACTION_SHOP_EXIT, run_id, generation, revision),
        "label": errors.LABEL_EXIT,
    })
    return components


def _shop_label(item: dict) -> str:
    """진열품의 이름. `item_ref` 는 카드와 즉시 효과 두 모양을 모두 담는다."""
    import json

    try:
        payload = json.loads(item.get("item_ref") or "{}")
    except (TypeError, ValueError):
        return "물건"
    return str(payload.get("name") or payload.get("card_id")
               or payload.get("kind") or "물건")


# =====================================================================
# 이벤트 (§3.3)
# =====================================================================
def event(db: Database, run_id: int, options: dict) -> list[dict]:
    """분기 버튼. 분기가 없는 즉시형 이벤트에는 누를 것이 없다."""
    branches = options.get("branches") or []
    if not branches:
        return []
    generation, revision = _surface(db, run_id)
    return [{
        "type": "button",
        "custom_id": cid.build(cid.ACTION_EVENT_BRANCH, run_id, generation,
                               revision, str(index)),
        "label": str(branch.get("label", f"선택 {index + 1}"))[:80],
    } for index, branch in enumerate(branches[:MAX_BUTTONS])]
