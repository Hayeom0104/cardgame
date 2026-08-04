"""장비 시스템 (설계 문서 §8).

확정 사항:
* 획득: 허브 상점 구매(§7.2). 그 외 경로(맵 드랍, 가챠)는 TBD.
* 강화: 카드 조각/와일드카드와 **구분되는 전용 재료**를 쓴다.
* 블루아카이브식 티어제.

TBD (§8, §13): 전용 재료의 이름과 획득처, 캐릭터별 슬롯 수, 장비 등급,
스탯 효과 상세. 재료는 `User.equip_material` 필드에 식별자만 잡아두었고
표시 이름은 balance.EQUIP_MATERIAL_DISPLAY_NAME 에서 온다.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..db.models import Equipment, User, UserEquipment


class EquipmentError(Exception):
    pass


def stat_bonus(session: Session, user: User, character_code: str) -> dict[str, int]:
    """해당 캐릭터에 장착된 장비들의 스탯 합계.

    티어당 base_stats 가 선형 누적된다 (티어 N = base_stats × N). 정확한
    성장 곡선은 TBD.
    """
    rows = session.scalars(
        select(UserEquipment).where(
            UserEquipment.user_id == user.id, UserEquipment.equipped_on == character_code
        )
    ).all()
    if not rows:
        return {}

    codes = [r.equipment_code for r in rows]
    masters = {
        e.code: e for e in session.scalars(select(Equipment).where(Equipment.code.in_(codes))).all()
    }

    total: dict[str, int] = {}
    for row in rows:
        master = masters.get(row.equipment_code)
        if master is None:
            continue
        for stat, value in (master.base_stats or {}).items():
            total[stat] = total.get(stat, 0) + int(value) * row.tier
    return total


def equip(session: Session, user: User, equipment_code: str, character_code: str | None) -> str:
    """장비를 캐릭터에 장착하거나(character_code) 해제한다(None)."""
    row = session.scalar(
        select(UserEquipment).where(
            UserEquipment.user_id == user.id, UserEquipment.equipment_code == equipment_code
        )
    )
    if row is None:
        raise EquipmentError("보유하지 않은 장비입니다.")
    row.equipped_on = character_code
    session.flush()
    return character_code or ""


def tier_up(session: Session, user: User, equipment_code: str) -> dict:
    """§8 티어 상승 — 전용 강화 재료를 소모한다."""
    row = session.scalar(
        select(UserEquipment).where(
            UserEquipment.user_id == user.id, UserEquipment.equipment_code == equipment_code
        )
    )
    if row is None:
        raise EquipmentError("보유하지 않은 장비입니다.")

    master = session.scalar(select(Equipment).where(Equipment.code == equipment_code))
    if master is None:
        raise EquipmentError("장비 데이터를 찾을 수 없습니다.")

    cap = min(master.max_tier, balance.EQUIPMENT_MAX_TIER)
    if row.tier >= cap:
        raise EquipmentError(f"{master.name} 은(는) 이미 최대 티어({cap})입니다.")

    target = row.tier + 1
    cost = balance.EQUIP_TIER_UP_COST.get(target)
    if cost is None:
        raise EquipmentError(f"티어 {target} 강화 비용이 정의되어 있지 않습니다.")
    if user.equip_material < cost:
        raise EquipmentError(
            f"{balance.EQUIP_MATERIAL_DISPLAY_NAME}이(가) 부족합니다. "
            f"(필요 {cost}, 보유 {user.equip_material})"
        )

    user.equip_material -= cost
    row.tier = target
    session.flush()
    return {"name": master.name, "tier": target, "cost": cost}
