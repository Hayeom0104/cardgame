"""허브 화면 중 컴포넌트를 가진 것들 — §16.2.2 준비 화면과 §5 뽑기 화면.

§19.2의 `custom_id` 형식(`dko:<action>:<run_id36>:<gen>:<rev>:<payload>`)은 **런
안** 컴포넌트를 위한 것이다. 여기 두 화면은 런 밖에서 동작하므로 run_id도
generation도 revision도 없다. 그래서 별도 접두사(`dko:prep:`, `dko:gacha:`)를
쓰고, 다섯 게이트(§1.3.10) 대신 각 화면에 맞는 검사를 한다:

    · OWNERSHIP  이벤트의 user_id가 곧 대상이다 — 남의 계정을 지목할 수 없다
    · LIVENESS   준비 화면은 진행 중인 런이 없을 때만, 뽑기는 배너 가용성으로
    · LEGALITY   선택한 월드/캐릭터/배너가 지금도 유효한지
    · 원자성     뽑기는 §17.5가 한 로컬 트랜잭션으로 보장한다

준비 화면 [1]-[4]는 **run row를 만들지 않는다** (§16.2.2). 중간 선택은
`run_drafts`에 모이고, 런은 [5] MATERIALIZE에서 비로소 생긴다.
"""

from __future__ import annotations

import json
import logging

from app.api import errors, visuals
from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import gacha
from app.engine import lifecycle as lc
from app.engine import progression as pg

logger = logging.getLogger(__name__)

PREP_PREFIX = "dko:prep:"
GACHA_PREFIX = "dko:gacha:"


# =====================================================================
# §16.2.2 준비 화면
# =====================================================================
def load_draft(db: Database, user_id: int) -> dict:
    row = db.one("SELECT * FROM run_drafts WHERE user_id = ?", (user_id,))
    if row is None:
        return {"world_id": None, "party": [], "passives": []}
    return {
        "world_id": row["world_id"],
        "party": json.loads(row["party_json"]),
        "passives": json.loads(row["passive_json"]),
    }


def save_draft(db: Database, user_id: int, draft: dict) -> None:
    db.execute(
        "INSERT INTO run_drafts (user_id, world_id, party_json, passive_json, "
        "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "world_id = excluded.world_id, party_json = excluded.party_json, "
        "passive_json = excluded.passive_json, updated_at = excluded.updated_at",
        (user_id, draft.get("world_id"),
         json.dumps(draft.get("party", []), ensure_ascii=False),
         json.dumps(draft.get("passives", []), ensure_ascii=False), utcnow()),
    )


def clear_draft(db: Database, user_id: int) -> None:
    """[1]-[4]를 그만두는 데는 아무 비용도 들지 않는다 (§16.2.2)."""
    db.execute("DELETE FROM run_drafts WHERE user_id = ?", (user_id,))


def unlocked_worlds(db: Database, user_id: int, content_version_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT wu.world_id, w.name, w.is_tutorial FROM world_unlocks wu "
        "JOIN worlds w ON w.world_id = wu.world_id AND w.content_version_id = ? "
        "WHERE wu.user_id = ? ORDER BY w.sequence_index",
        (content_version_id, user_id))]


def owned_characters(db: Database, user_id: int,
                     content_version_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT oc.character_id, oc.star_rank, c.name, c.element, c.job_role "
        "FROM owned_characters oc JOIN characters c "
        "ON c.character_id = oc.character_id AND c.content_version_id = ? "
        "WHERE oc.user_id = ? ORDER BY oc.acquired_at", (content_version_id, user_id))]


def world_select_screen(db: Database, user_id: int,
                        content_version_id: int) -> dict:
    """[1] WORLD SELECT — `world_unlocks`에서 고른다 (§3.6)."""
    worlds = unlocked_worlds(db, user_id, content_version_id)
    if not worlds:
        return {"action": "reply_ephemeral", "content": errors.TUTORIAL_NOT_CLEARED}

    return {
        "action": "reply_ephemeral",
        "content": "**준비 화면** — [1] 월드 선택",
        "components": [{
            "type": "string_select",
            "custom_id": f"{PREP_PREFIX}world",
            "placeholder": "월드를 선택하세요",
            "options": [{"label": world["name"], "value": world["world_id"]}
                        for world in worlds[:25]],
        }],
    }


def party_select_screen(db: Database, user_id: int, content_version_id: int,
                        draft: dict) -> dict:
    """[2] PARTY SELECT — `party_slots`만큼 고른다. 본편은 ≥2, 중복 불가."""
    account = db.one("SELECT party_slots FROM accounts WHERE user_id = ?", (user_id,))
    characters = owned_characters(db, user_id, content_version_id)
    slots = int(account["party_slots"])

    world = db.one(
        "SELECT is_tutorial FROM worlds WHERE content_version_id = ? AND world_id = ?",
        (content_version_id, draft["world_id"]))
    is_tutorial = bool(world["is_tutorial"]) if world else False

    if not is_tutorial and len(characters) < 2:
        # 두 번째 캐릭터는 §5.10 첫 뽑기 보장으로 들어온다.
        return {"action": "reply_ephemeral",
                "content": f"{errors.PARTY_TOO_SMALL}\n`!덱아웃 뽑기`로 동료를 모아보세요."}

    minimum = 1 if is_tutorial else 2
    return {
        "action": "edit",
        "content": (f"**준비 화면** — [2] 파티 선택 "
                    f"({minimum}~{slots}명, 중복 불가)"),
        "components": [{
            "type": "string_select",
            "custom_id": f"{PREP_PREFIX}party",
            "placeholder": "파티원을 선택하세요",
            "min_values": minimum,
            "max_values": min(slots, len(characters)),
            "options": [
                {"label": f"{entry['name']} {'★' * entry['star_rank']}",
                 "description": f"{entry['element']} · {entry['job_role']}",
                 "value": entry["character_id"]}
                for entry in characters[:25]
            ],
        }],
    }


def passive_select_screen(db: Database, user_id: int, content_version_id: int,
                          draft: dict) -> dict:
    """[3] PASSIVE SELECT — `passive_slots`까지, 더 적어도 된다 (§6)."""
    account = db.one("SELECT passive_slots FROM accounts WHERE user_id = ?",
                     (user_id,))
    slots = int(account["passive_slots"])

    # 고를 것이 없으면 이 단계는 건너뛴다 — 빈 화면을 띄울 이유가 없다.
    # 목록의 정의는 `lifecycle.selectable_passives` 한 곳에만 있고,
    # `validate_build`도 같은 목록으로 검사한다.
    passives = lc.selectable_passives(db, user_id, content_version_id)
    if not passives:
        return confirm_screen(db, user_id, content_version_id, draft)

    return {
        "action": "edit",
        "content": f"**준비 화면** — [3] 패시브 선택 (최대 {slots}개, 생략 가능)",
        "components": [
            {"type": "string_select", "custom_id": f"{PREP_PREFIX}passive",
             "placeholder": "패시브를 선택하세요",
             "min_values": 0, "max_values": min(slots, len(passives)),
             "options": [{"label": row["name"], "value": row["card_id"]}
                         for row in passives[:25]]},
            {"type": "button", "custom_id": f"{PREP_PREFIX}skip_passive",
             "label": "건너뛰기"},
        ],
    }


def confirm_screen(db: Database, user_id: int, content_version_id: int,
                   draft: dict) -> dict:
    """[4] CONFIRM — 스냅샷될 빌드를 그대로 보여주고, 아무 비용도 들지 않는다.

    여기 표시되는 값이 §16.2.3에서 얼려질 바로 그 값이다.
    """
    from app.engine import stats

    balance = Balance(db, content_version_id)
    account = db.one("SELECT stat_research_step FROM accounts WHERE user_id = ?",
                     (user_id,))
    world = db.one("SELECT name FROM worlds WHERE content_version_id = ? "
                   "AND world_id = ?", (content_version_id, draft["world_id"]))

    lines = ["**준비 화면** — [4] 확정",
             f"월드: {world['name'] if world else draft['world_id']}"]
    #: 글자와 그림이 같은 값을 보여주도록 한 번만 계산해 둘 다에 쓴다.
    members: list[dict] = []
    for character_id in draft["party"]:
        row = db.one(
            "SELECT oc.star_rank, c.name, c.job_role, c.element FROM owned_characters oc "
            "JOIN characters c ON c.character_id = oc.character_id "
            "AND c.content_version_id = ? WHERE oc.user_id = ? AND oc.character_id = ?",
            (content_version_id, user_id, character_id))
        if row is None:
            continue
        snapshot = stats.BuildSnapshot(
            character_id=character_id, star_rank=int(row["star_rank"]),
            research_stat_step=int(account["stat_research_step"]),
            job_role=row["job_role"],
            equipment_flat=_equipment_flat(db, user_id, character_id,
                                           content_version_id),
        )
        block = stats.character_stats(balance, snapshot)
        lines.append(
            f"　{row['name']} {'★' * int(row['star_rank'])} · {row['element']} · "
            f"HP {block['hp']} / 공 {block['atk']} / 방 {block['def']} / 속 {block['spd']}")
        members.append({
            "character_id": character_id, "name": row["name"],
            "star_rank": int(row["star_rank"]), "element": row["element"],
            "job_role": row["job_role"], **block,
        })

    if draft["passives"]:
        lines.append(f"패시브: {len(draft['passives'])}개")

    return {
        "action": "edit",
        "content": "\n".join(lines),
        "components": [
            {"type": "button", "custom_id": f"{PREP_PREFIX}confirm", "label": "확정"},
            {"type": "button", "custom_id": f"{PREP_PREFIX}cancel", "label": "취소"},
        ],
        # 여기 보이는 값이 §16.2.3에서 그대로 얼려진다.
        "attachments": visuals.prep(
            db, balance, user_id=user_id, content_version_id=content_version_id,
            world_name=world["name"] if world else draft["world_id"],
            party=members),
    }


def _equipment_flat(db: Database, user_id: int, character_id: str,
                    content_version_id: int) -> dict[str, int]:
    from app.engine.encounter import _equipment_flat as compute

    equipment = {}
    for row in db.query(
        "SELECT equipment_def_id, tier, equipped_slot FROM owned_equipment "
        "WHERE user_id = ? AND equipped_character_id = ?", (user_id, character_id)
    ):
        equipment[row["equipped_slot"]] = {"def_id": row["equipment_def_id"],
                                           "tier": row["tier"]}
    return compute(db, content_version_id, json.dumps(equipment))


def handle_prep(db: Database, balance: Balance, user_id: int, custom_id: str,
                values: list[str], content_version_id: int) -> dict:
    """준비 화면의 컴포넌트 제출을 처리한다.

    [1]-[4] 어디서든 진행 중인 런이 생겼다면(다른 창에서 시작했거나) 거절한다
    — §16.3의 계정당 하나 규칙은 여기서도 지켜져야 한다.
    """
    step = custom_id[len(PREP_PREFIX):]
    draft = load_draft(db, user_id)

    if step == "cancel":
        clear_draft(db, user_id)
        return {"action": "edit", "content": "준비를 취소했습니다.", "components": []}

    if lc.active_run_for(db, user_id) is not None:
        clear_draft(db, user_id)
        return {"action": "edit", "content": errors.RUN_ALREADY_ACTIVE,
                "components": []}

    if step == "world":
        if not values:
            return {"action": "edit", "content": errors.ILLEGAL_STATE}
        chosen = values[0]
        legal = {world["world_id"] for world in
                 unlocked_worlds(db, user_id, content_version_id)}
        if chosen not in legal:
            return {"action": "edit", "content": errors.ILLEGAL_STATE}
        draft = {**draft, "world_id": chosen, "party": [], "passives": []}
        save_draft(db, user_id, draft)
        return party_select_screen(db, user_id, content_version_id, draft)

    if step == "party":
        if draft["world_id"] is None:
            return world_select_screen(db, user_id, content_version_id)
        owned = {entry["character_id"] for entry in
                 owned_characters(db, user_id, content_version_id)}
        chosen = list(dict.fromkeys(values))     # 중복은 여기서 접힌다
        if not chosen or not set(chosen) <= owned:
            return {"action": "edit", "content": errors.ILLEGAL_STATE}
        draft = {**draft, "party": chosen}
        save_draft(db, user_id, draft)
        return passive_select_screen(db, user_id, content_version_id, draft)

    if step in ("passive", "skip_passive"):
        if not draft["party"]:
            return world_select_screen(db, user_id, content_version_id)
        chosen = list(dict.fromkeys(values)) if step == "passive" else []
        # `custom_id`는 위조될 수 있으니, 화면이 실제로 보여준 목록과 대조한다.
        legal = {row["card_id"] for row in
                 lc.selectable_passives(db, user_id, content_version_id)}
        if not set(chosen) <= legal:
            return {"action": "edit", "content": errors.ILLEGAL_STATE}
        draft = {**draft, "passives": chosen}
        save_draft(db, user_id, draft)
        return confirm_screen(db, user_id, content_version_id, draft)

    if step == "confirm":
        return materialize(db, balance, user_id, draft, content_version_id)

    return {"action": "edit", "content": errors.ILLEGAL_STATE}


def materialize(db: Database, balance: Balance, user_id: int, draft: dict,
                content_version_id: int) -> dict:
    """[5] MATERIALIZE — 런은 여기서 비로소 존재하게 된다. 이어서 [6] SURFACE.

    두 단계를 갈라놓으면 안 된다. `runs.thread_id`를 쓰는 유일한 경로가
    `message_delivery_result` 콜백(§1.3.3)이므로, 스레드 생성을 요청하지 않은
    런은 스레드도 화면도 없이 계정만 점유한다 — §16.3의 계정당 하나 규칙 탓에
    새 런을 시작할 수도 없다.
    """
    world = db.one(
        "SELECT is_tutorial FROM worlds WHERE content_version_id = ? AND world_id = ?",
        (content_version_id, draft["world_id"]))
    request = lc.RunBuildRequest(
        user_id=user_id,
        world_id=draft["world_id"],
        party_character_ids=draft["party"],
        passive_card_ids=draft["passives"],
        is_tutorial=bool(world["is_tutorial"]) if world else False,
    )
    try:
        run_id = lc.create_run(db, balance, request, content_version_id)
    except lc.LifecycleError as error:
        return {"action": "edit", "content": str(error)}

    clear_draft(db, user_id)
    return {**surface_request(db, run_id, user_id), "action": "edit",
            "components": []}


def surface_request(db: Database, run_id: int, user_id: int) -> dict:
    """[6] SURFACE — 비공개 스레드 생성을 요청한다 (§1.3.5).

    전송 전에 delivery intent를 기록한다 (§1.3.3, B-13): 콜백은
    `surface_generation`도 `presentation_revision`도 싣고 오지 않으므로,
    그 둘은 우리가 미리 적어 두어야만 알 수 있다.
    """
    from app.central import delivery

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    request_id = delivery.mint_request_id("thread")
    delivery.record_intent(
        db, request_id=request_id, run_id=run_id, purpose="canonical",
        surface_generation=run["surface_generation"],
        presentation_revision=run["presentation_revision"],
    )
    return {
        "content": "런을 시작합니다.",
        "metadata": {"request_id": request_id},
        # 응답 경로의 `create_thread`가 아니라 서비스 주도 API를 쓴다 —
        # 전자는 공개 스레드를 만든다 (§1.3.5).
        "thread_request": {
            "logical_session_id": run["logical_session_id"],
            "surface_generation": run["surface_generation"],
            "owner_user_id": user_id,
            "thread_name": f"덱아웃 - {user_id}",
        },
        "run_id": run_id,
    }


# =====================================================================
# §5 뽑기 화면
# =====================================================================
def gacha_screen(db: Database, balance: Balance, user_id: int,
                 content_version_id: int) -> dict:
    """`!덱아웃 뽑기` — 배너 목록과 카르타 잔액.

    §5.10.2: 첫 뽑기 보장이 소비되기 전에는 **상시 배너만** 뽑을 수 있고,
    한정 배너는 숨겨진다. 새 계정을 정확히 첫 열 번의 뽑기 동안만 제한한다.
    """
    account = db.one("SELECT carta, first_pull_guarantee_used, "
                     "first_pull_results_count FROM accounts WHERE user_id = ?",
                     (user_id,))
    banners = [dict(row) for row in db.query(
        "SELECT * FROM banners WHERE content_version_id = ? ORDER BY banner_type, "
        "banner_id", (content_version_id,))]
    available = [banner for banner in banners
                 if gacha.banner_is_available(db, user_id, banner)]

    single = int(balance.get("gacha_cost_single"))
    ten = int(balance.get("gacha_cost_ten"))

    lines = [f"**뽑기** — 카르타 {account['carta']}"]
    if not account["first_pull_guarantee_used"]:
        remaining = (int(balance.get("first_pull_guarantee_window"))
                     - int(account["first_pull_results_count"]))
        lines.append(
            f"첫 {remaining}회 안에 캐릭터가 반드시 나옵니다. "
            "그때까지 한정 배너는 열리지 않습니다.")

    components = []
    for banner in available:
        label = "상시 배너" if banner["banner_type"] == "standard" else "한정 배너"
        if banner["pickup_target_id"]:
            label += f" (픽업: {banner['pickup_target_id']})"
        lines.append(f"· {label}")
        components.append({
            "type": "button",
            "custom_id": f"{GACHA_PREFIX}{banner['banner_id']}:single",
            "label": f"단차 {single}",
        })
        components.append({
            "type": "button",
            "custom_id": f"{GACHA_PREFIX}{banner['banner_id']}:ten",
            "label": f"10연 {ten}",
        })

    # 배너 그림은 첫 배너 것을 붙인다 — 확률과 천장을 글자로만 보여주면
    # 무엇에 돈을 쓰는지 한눈에 들어오지 않는다.
    art = (visuals.banner(db, balance, available[0], user_id=user_id,
                          carta=int(account["carta"])) if available else [])
    return {"action": "reply_ephemeral", "content": "\n".join(lines),
            "components": components, "attachments": art}


def handle_gacha(db: Database, balance: Balance, user_id: int, custom_id: str,
                 content_version_id: int, event_id: str | None = None) -> dict:
    """뽑기 버튼. §17.5가 한 로컬 트랜잭션을 보장하므로 여기서는 결과만 정리한다.

    `event_id`가 있으면 그것으로 `gacha_id`를 유도한다. 중앙봇이 같은 이벤트를
    재전송하면 §5.9의 재생 경로가 `results_json`을 그대로 돌려주므로, 다시
    뽑히지도 다시 과금되지도 않는다 — id를 매번 새로 만들면 그 보호가 통째로
    작동하지 않는다.
    """
    payload = custom_id[len(GACHA_PREFIX):]
    banner_id, _, pull_kind = payload.rpartition(":")
    if pull_kind not in ("single", "ten"):
        return {"action": "edit", "content": errors.ILLEGAL_STATE}

    gacha_id = f"evt:{event_id}" if event_id else None
    try:
        outcome = gacha.pull(db, balance, user_id=user_id, banner_id=banner_id,
                             pull_kind=pull_kind,
                             content_version_id=content_version_id,
                             gacha_id=gacha_id)
    except gacha.GachaError as error:
        message = (errors.INSUFFICIENT_CURRENCY if "insufficient" in str(error)
                   else errors.ILLEGAL_STATE)
        logger.info("gacha rejected for %s: %s", user_id, error)
        return {"action": "edit", "content": message}

    results = [{"kind": result.kind, "entity_id": result.entity_id,
                "is_duplicate": result.is_duplicate,
                "fragments": result.fragments,
                "forced": result.forced_by_guarantee}
               for result in outcome.results]
    return {"action": "edit",
            "content": format_results(db, outcome, content_version_id),
            "attachments": visuals.gacha_results(db, content_version_id, results)}


def format_results(db: Database, outcome: gacha.GachaOutcome,
                   content_version_id: int) -> str:
    band_label = {gacha.BAND_TOP: "★★★", gacha.BAND_MID: "★★",
                  gacha.BAND_BASE: "★"}
    lines = [f"**뽑기 결과** ({len(outcome.results)}회)"]
    for result in outcome.results:
        name = _entity_name(db, result, content_version_id)
        mark = band_label.get(result.band, "")
        line = f"{mark} {name}"
        if result.is_duplicate:
            # §5.2 — 중복 캐릭터는 조각과 와일드카드를 **둘 다** 준다 (🟡 R-4).
            parts = [f"조각 +{result.fragments}"]
            if result.wildcards:
                parts.append(f"와일드카드 +{result.wildcards}")
            line += f" (중복 · {' · '.join(parts)})"
        if result.forced_by_guarantee:
            line += " ← 첫 뽑기 보장"
        lines.append(line)
    return "\n".join(lines)


def _entity_name(db: Database, result: gacha.PullResult,
                 content_version_id: int) -> str:
    table, column = (("characters", "character_id") if result.kind == "character"
                     else ("cards", "card_id"))
    row = db.one(
        f"SELECT name FROM {table} WHERE content_version_id = ? AND {column} = ?",
        (content_version_id, result.entity_id))
    return row["name"] if row else result.entity_id
