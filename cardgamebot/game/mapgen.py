"""로그라이크 맵 생성 (설계 문서 §3).

* 맵은 런마다 무작위 생성된다 (고정 캠페인 아님).
* 플레이어가 분기 중 다음 노드를 직접 고른다.
* 마지막 층은 보스 단일 노드 — 월드의 종착점.

노드 타입은 §3.1 의 확정 목록만 사용한다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .. import balance
from ..db.models import NodeType


@dataclass
class MapNode:
    id: str
    floor: int
    index: int
    type: str
    # 노드별 부가 데이터(전투 편성, 상점 재고 등)는 방문 시점에 채운다.
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "floor": self.floor,
            "index": self.index,
            "type": self.type,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MapNode":
        return cls(id=d["id"], floor=d["floor"], index=d["index"], type=d["type"], data=d.get("data", {}))


@dataclass
class GameMap:
    nodes: dict[str, MapNode]
    edges: dict[str, list[str]]
    floors: list[list[str]]

    # -------- 조회 --------

    def entry_nodes(self) -> list[MapNode]:
        return [self.nodes[nid] for nid in self.floors[0]]

    def next_nodes(self, node_id: str) -> list[MapNode]:
        return [self.nodes[nid] for nid in self.edges.get(node_id, [])]

    def boss_node(self) -> MapNode:
        return self.nodes[self.floors[-1][0]]

    # -------- 직렬화 --------

    def to_dict(self) -> dict:
        return {
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "edges": self.edges,
            "floors": self.floors,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameMap":
        return cls(
            nodes={k: MapNode.from_dict(v) for k, v in d["nodes"].items()},
            edges={k: list(v) for k, v in d["edges"].items()},
            floors=[list(f) for f in d["floors"]],
        )


def _pick_type(rng: random.Random, floor: int, used_on_floor: list[str]) -> str:
    if floor == 0 and balance.FIRST_FLOOR_ALL_COMBAT:
        return NodeType.COMBAT.value

    weights = dict(balance.NODE_WEIGHTS)
    # 같은 층에 동일 타입이 몰리면 선택의 의미가 없어지므로 가중치를 낮춘다.
    for t in used_on_floor:
        if t in weights:
            weights[t] = max(1, weights[t] // 4)
    # 휴식/상점이 연속으로 나오면 위험 관리 긴장감(§3)이 사라진다.
    types = list(weights)
    return rng.choices(types, weights=[weights[t] for t in types], k=1)[0]


def generate_map(seed: int, floors: int = balance.MAP_FLOORS) -> GameMap:
    """분기형 층 구조 맵을 생성한다.

    각 층은 2~3 노드, 인접 층끼리만 연결된다. 연결은 "모든 노드가 최소 하나의
    진입로와 진출로를 갖는다"를 보장해 막다른 길이 생기지 않게 만든다.
    """
    rng = random.Random(seed)
    nodes: dict[str, MapNode] = {}
    edges: dict[str, list[str]] = {}
    layout: list[list[str]] = []

    for floor in range(floors):
        if floor == floors - 1:
            # §3.1 보스는 월드의 종착 노드 — 항상 단독.
            node = MapNode(id=f"n{floor}_0", floor=floor, index=0, type=NodeType.BOSS.value)
            nodes[node.id] = node
            edges[node.id] = []
            layout.append([node.id])
            continue

        width = rng.randint(balance.MAP_MIN_BRANCH, balance.MAP_MAX_BRANCH)
        row: list[str] = []
        used: list[str] = []
        for index in range(width):
            ntype = _pick_type(rng, floor, used)
            used.append(ntype)
            node = MapNode(id=f"n{floor}_{index}", floor=floor, index=index, type=ntype)
            nodes[node.id] = node
            edges[node.id] = []
            row.append(node.id)
        layout.append(row)

    # 층간 간선 연결
    for floor in range(len(layout) - 1):
        current, nxt = layout[floor], layout[floor + 1]
        for i, nid in enumerate(current):
            # 위치가 가까운 노드로 1~2개 연결
            anchor = min(int(i * len(nxt) / len(current)), len(nxt) - 1)
            targets = {nxt[anchor]}
            if len(nxt) > 1 and rng.random() < 0.55:
                targets.add(nxt[min(anchor + 1, len(nxt) - 1)])
            edges[nid] = sorted(targets)

        # 진입로가 없는 다음 층 노드가 남지 않도록 보정
        reachable = {t for nid in current for t in edges[nid]}
        for nid in nxt:
            if nid not in reachable:
                donor = rng.choice(current)
                edges[donor] = sorted(set(edges[donor]) | {nid})

    return GameMap(nodes=nodes, edges=edges, floors=layout)
