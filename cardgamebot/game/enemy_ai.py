"""적 행동 선택.

주의 — 설계 문서 §2.7 / §13 에서 "적 AI 행동 패턴"은 미확정(TBD)이다.
여기 있는 것은 전투가 돌아가게 하는 **최소 구현**이며, 확정된 설계가 아니다:

* 행동은 `Enemy.moves` 의 가중치 랜덤으로 고른다.
* 대상은 살아있는 아군 중 랜덤. (어그로/패턴 개념 없음)

패턴이 확정되면 이 모듈만 교체하면 되고, CombatEngine 은 손대지 않는다.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from .entities import Unit

# 적 데이터에 moves 가 비어 있을 때 쓰는 최후의 기본 행동.
FALLBACK_MOVE: dict = {
    "name": "공격",
    "weight": 1,
    "target": "enemy_single",  # 적 입장에서 '적' = 플레이어
    "effects": [{"op": "damage", "amount": 0}],
}


def choose_move(enemy: Unit, moves: Sequence[dict], rng: random.Random) -> dict:
    """가중치 기반으로 행동 하나를 고른다."""
    pool = [m for m in (moves or []) if m.get("effects")]
    if not pool:
        return dict(FALLBACK_MOVE)
    weights = [max(1, int(m.get("weight", 1))) for m in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def choose_targets(
    move: dict, enemy: Unit, players: Sequence[Unit], enemies: Sequence[Unit], rng: random.Random
) -> list[Unit]:
    """행동의 target 표기에 따라 대상을 고른다.

    적 시점의 표기라 `enemy_*` 는 플레이어를, `ally_*` 는 다른 적을 가리킨다.
    """
    target = move.get("target", "enemy_single")
    alive_players = [u for u in players if u.alive]
    alive_enemies = [u for u in enemies if u.alive]

    if target == "enemy_all":
        return alive_players
    if target == "ally_all":
        return alive_enemies
    if target == "ally_single":
        return [rng.choice(alive_enemies)] if alive_enemies else []
    if target == "self":
        return [enemy]
    # 기본: enemy_single
    return [rng.choice(alive_players)] if alive_players else []
