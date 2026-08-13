"""연구 시스템 (설계 문서 §9).

확정 사항:
* 즉시 해금 — 대기 타이머 없음. 비용을 내는 즉시 적용된다.
* 전용 재화 없음 — 코인/카드 조각/와일드카드만 사용한다.
  (코인은 중앙봇 소유이므로 §1.1 에 따라 API 로 차감한다)
* 범위: 파티 슬롯 확장(§4.2), 패시브 슬롯 확장(§6), 스탯 강화, 신규 노드 해금.

전체 노드 목록과 비용은 TBD(§13) 이므로 코드가 아니라 `research_nodes`
테이블 데이터로 관리한다. seed.py 의 값은 임시 예시다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..core.central_client import CentralAPIError, get_central_client
from ..db.models import ResearchNode, User, UserResearch


class ResearchError(Exception):
    pass


@dataclass
class AccountBonuses:
    """연구로 획득한 계정 단위 영구 효과."""

    party_slots: int = balance.BASE_PARTY_SLOTS
    passive_slots: int = balance.BASE_PASSIVE_SLOTS
    stat_percent: dict[str, float] = field(default_factory=dict)
    unlocked_node_types: set[str] = field(default_factory=set)

    def stat_multiplier(self, stat: str) -> float:
        return 1.0 + self.stat_percent.get(stat, 0.0)


def _levels(session: Session, user: User) -> dict[str, int]:
    rows = session.scalars(select(UserResearch).where(UserResearch.user_id == user.id)).all()
    return {r.node_code: r.level for r in rows}


def account_bonuses(session: Session, user: User) -> AccountBonuses:
    """유저의 연구 해금 상태를 실제 게임 수치로 환산한다."""
    bonuses = AccountBonuses()
    levels = _levels(session, user)
    if not levels:
        return bonuses

    nodes = session.scalars(
        select(ResearchNode).where(ResearchNode.code.in_(list(levels)))
    ).all()

    for node in nodes:
        level = levels.get(node.code, 0)
        if level <= 0:
            continue
        effect = node.effect or {}
        op = effect.get("op")

        if op == "party_slot":
            # value = 이 노드가 보장하는 슬롯 수 (누적이 아니라 목표치)
            bonuses.party_slots = max(bonuses.party_slots, int(effect.get("value", 1)))
        elif op == "passive_slot":
            bonuses.passive_slots = max(bonuses.passive_slots, int(effect.get("value", 2)))
        elif op == "stat":
            stat = effect.get("stat", "hp")
            per_level = float(effect.get("percent", 0.0))
            bonuses.stat_percent[stat] = bonuses.stat_percent.get(stat, 0.0) + per_level * level
        elif op == "unlock_node":
            bonuses.unlocked_node_types.add(str(effect.get("node")))

    # 설계상 상한을 넘지 않게 고정 (§4.1, §6)
    bonuses.party_slots = min(bonuses.party_slots, balance.MAX_PARTY_SLOTS)
    bonuses.passive_slots = min(bonuses.passive_slots, balance.MAX_PASSIVE_SLOTS)
    return bonuses


def node_cost(node: ResearchNode, target_level: int) -> dict[str, int]:
    """target_level (1-based) 로 올리는 데 드는 비용."""
    costs = node.costs or []
    idx = target_level - 1
    if idx < 0 or idx >= len(costs):
        raise ResearchError(f"`{node.code}` 의 레벨 {target_level} 비용이 정의되어 있지 않습니다.")
    raw = costs[idx]
    return {
        "coin": int(raw.get("coin", 0)),
        "fragment": int(raw.get("fragment", 0)),
        "wildcard": int(raw.get("wildcard", 0)),
    }


def list_nodes(session: Session, user: User) -> list[dict]:
    """연구 트리 상태를 한눈에 보여주기 위한 목록."""
    levels = _levels(session, user)
    nodes = session.scalars(
        select(ResearchNode).where(ResearchNode.is_active.is_(True)).order_by(ResearchNode.id)
    ).all()

    result: list[dict] = []
    for node in nodes:
        level = levels.get(node.code, 0)
        maxed = level >= node.max_level
        missing = [req for req in (node.requires or []) if levels.get(req, 0) <= 0]
        entry = {
            "code": node.code,
            "name": node.name,
            "description": node.description,
            "level": level,
            "max_level": node.max_level,
            "maxed": maxed,
            "locked_by": missing,
            "next_cost": None if maxed else node_cost(node, level + 1),
        }
        result.append(entry)
    return result


async def unlock(session: Session, user: User, code: str) -> dict:
    """§9 즉시 해금. 비용 지불 후 바로 효과가 적용된다."""
    node = session.scalar(select(ResearchNode).where(ResearchNode.code == code))
    if node is None or not node.is_active:
        raise ResearchError(f"연구 `{code}` 를 찾을 수 없습니다.")

    levels = _levels(session, user)
    current = levels.get(code, 0)
    if current >= node.max_level:
        raise ResearchError(f"`{node.name}` 은(는) 이미 최대 레벨입니다.")

    missing = [req for req in (node.requires or []) if levels.get(req, 0) <= 0]
    if missing:
        raise ResearchError(f"선행 연구가 필요합니다: {', '.join(missing)}")

    cost = node_cost(node, current + 1)

    # 로컬 재화 검사 먼저 — 코인 차감(외부 호출)을 되돌릴 필요가 없게 한다.
    if user.fragments < cost["fragment"]:
        raise ResearchError(f"카드 조각이 부족합니다. (필요 {cost['fragment']}, 보유 {user.fragments})")
    if user.wildcards < cost["wildcard"]:
        raise ResearchError(f"와일드카드가 부족합니다. (필요 {cost['wildcard']}, 보유 {user.wildcards})")

    if cost["coin"] > 0:
        try:
            await get_central_client().spend_currency(
                user.discord_id, cost["coin"],
                idempotency_key=f"deckout:research:{user.id}:{code}:{current + 1}",
                reason=f"research:{code}"
            )
        except CentralAPIError as exc:
            raise ResearchError(str(exc)) from exc

    user.fragments -= cost["fragment"]
    user.wildcards -= cost["wildcard"]

    row = session.scalar(
        select(UserResearch).where(
            UserResearch.user_id == user.id, UserResearch.node_code == code
        )
    )
    if row is None:
        row = UserResearch(user_id=user.id, node_code=code, level=1)
        session.add(row)
    else:
        row.level = current + 1

    session.flush()
    return {"code": code, "name": node.name, "level": current + 1, "cost": cost}
