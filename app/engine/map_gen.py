"""§3.5.3 — the canonical map generation algorithm.

v6.1 listed constraints but no procedure, retry bound, or fallback, so two
implementations could both pass the validator while producing materially
different maps. This module is that procedure.

A generation failure must never block a player: after MAX_ATTEMPTS the
FALLBACK_TEMPLATE — a hardcoded, pre-validated graph — is used and the event
logged. The run still starts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.content.balance import Balance
from app.db.connection import Database
from app.engine.rng import JournaledRng, key_map_gen

logger = logging.getLogger(__name__)

COMBAT = "전투"
EVENT = "이벤트"
REWARD = "보상"
REST = "휴식"
SHOP = "상점"
BOSS = "보스"

#: Node types whose placement carries a constraint (§3.5.3 step 5).
CONSTRAINED_TYPES = (SHOP, REST)


@dataclass
class GeneratedMap:
    nodes: list[dict] = field(default_factory=list)   # {node_index, depth, node_type}
    edges: list[tuple[int, int]] = field(default_factory=list)
    boss_index: int = 0
    used_fallback: bool = False
    attempts: int = 0

    def out_degree(self, node_index: int) -> int:
        return sum(1 for source, _ in self.edges if source == node_index)


def generate_map(rng: JournaledRng, balance: Balance) -> GeneratedMap:
    """`generate_map(rng, MAX_ATTEMPTS = 16)`.

    All randomness flows through one journaled draw so a recovery replays the
    identical map (§16.4) rather than rolling a fresh one.
    """
    depth_structure = balance.get("map_depth_structure")
    max_attempts = int(balance.get("map_generation_attempts"))
    shuffle_attempts = int(balance.get("map_node_shuffle_attempts"))
    quota = dict(balance.get("map_node_quota"))

    # One journaled draw seeds the whole generation; replays reuse it verbatim.
    seed = rng.draw(key_map_gen(), lambda r: r.getrandbits(63))
    import random

    for attempt in range(1, max_attempts + 1):
        local = random.Random(seed + attempt)
        result = _attempt(local, depth_structure)
        if result is None:
            continue
        nodes, edges = result
        assigned = _assign_node_types(local, nodes, depth_structure, quota,
                                      shuffle_attempts)
        generated = GeneratedMap(nodes=assigned, edges=edges, attempts=attempt)
        generated.boss_index = len(assigned) - 1
        return generated

    logger.warning("map generation exhausted %d attempts; using FALLBACK_TEMPLATE",
                   max_attempts)
    return _fallback_template(depth_structure, quota, shuffle_attempts)


# =====================================================================
# Steps 1-3
# =====================================================================
def _attempt(rng, depth_structure: list[int]):
    # --- 1. Nodes: indexed left-to-right within each depth ---
    nodes: list[dict] = []
    by_depth: list[list[int]] = []
    index = 0
    for depth, count in enumerate(depth_structure, start=1):
        row = []
        for _ in range(count):
            nodes.append({"node_index": index, "depth": depth, "node_type": None})
            row.append(index)
            index += 1
        by_depth.append(row)

    # --- 2. Edges: monotone, non-crossing ---
    edges: list[tuple[int, int]] = []
    intervals: list[list[tuple[int, int]]] = []
    for d in range(len(depth_structure) - 1):
        source_row, target_row = by_depth[d], by_depth[d + 1]
        a, b = len(source_row), len(target_row)
        ranges: list[tuple[int, int]] = []
        for i in range(a):
            if a == 1:
                lo, hi = 0, b - 1
            else:
                lo = (i * (b - 1)) // (a - 1)
                hi = ((i + 1) * (b - 1)) // (a - 1)
            hi = min(hi, b - 1)
            hi = max(hi, lo)
            ranges.append((lo, hi))

        # widen: an out-degree-1 node gains ONE rng-chosen free neighbour that
        # preserves monotonicity. Skipped at d in {5,6} where the graph
        # converges toward the single pre-boss node.
        depth_number = d + 1
        if depth_number not in (5, 6):
            ranges = _widen(rng, ranges, b)

        intervals.append(ranges)
        for i, (lo, hi) in enumerate(ranges):
            for j in range(lo, hi + 1):
                edges.append((source_row[i], target_row[j]))

    if not _validate(nodes, edges, by_depth, depth_structure):
        return None
    return nodes, edges


def _widen(rng, ranges: list[tuple[int, int]], b: int) -> list[tuple[int, int]]:
    widened = list(ranges)
    for i, (lo, hi) in enumerate(widened):
        if hi - lo + 1 != 1:
            continue
        candidates = []
        if lo - 1 >= 0 and (i == 0 or lo - 1 >= widened[i - 1][0]):
            candidates.append((lo - 1, hi))
        if hi + 1 <= b - 1 and (i == len(widened) - 1 or hi + 1 <= widened[i + 1][1]):
            candidates.append((lo, hi + 1))
        if candidates:
            widened[i] = candidates[rng.randrange(len(candidates))]
    return widened


def _validate(nodes: list[dict], edges: list[tuple[int, int]],
              by_depth: list[list[int]], depth_structure: list[int]) -> bool:
    """Step 3.

    Convergence note: branch width 2-3 is guaranteed at depths 1-4. Depths
    5→6→7 converge toward the single pre-boss rest node, so out-degree 1 is
    expected there and is not a validation failure.
    """
    outgoing: dict[int, set[int]] = {node["node_index"]: set() for node in nodes}
    incoming: dict[int, set[int]] = {node["node_index"]: set() for node in nodes}
    for source, target in edges:
        outgoing[source].add(target)
        incoming[target].add(source)

    last_depth_index = len(depth_structure) - 1
    terminal = set(by_depth[last_depth_index])

    for node in nodes:
        index, depth = node["node_index"], node["depth"]
        degree = len(outgoing[index])
        if index not in terminal:
            if degree < 1 or degree > 3:
                return False
            if depth <= 4 and not (2 <= degree <= 3):
                return False

    # every node reachable from depth 1
    reachable = set(by_depth[0])
    frontier = list(by_depth[0])
    while frontier:
        current = frontier.pop()
        for target in outgoing[current]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    if len(reachable) != len(nodes):
        return False

    # the BOSS (reached from the terminal depth) reachable from every node
    can_reach_end = set(terminal)
    changed = True
    while changed:
        changed = False
        for source, target in edges:
            if target in can_reach_end and source not in can_reach_end:
                can_reach_end.add(source)
                changed = True
    return len(can_reach_end) == len(nodes)


# =====================================================================
# Step 5 — node types
# =====================================================================
def _assign_node_types(rng, nodes: list[dict], depth_structure: list[int],
                       quota: dict[str, int], shuffle_attempts: int) -> list[dict]:
    """
        depth 1 node  <- 전투     (a shop first would leave nothing to buy with)
        depth 7 node  <- 휴식     (guaranteed rest before the boss)
        Remaining 13 nodes take from the residual quota
        assigned by seeded shuffle, rejecting placements where:
            · 상점 sits before depth 3
            · two 휴식 occupy adjacent depths
    """
    nodes = [dict(node) for node in nodes]
    last_depth = len(depth_structure)
    first = next(node for node in nodes if node["depth"] == 1)
    last = next(node for node in nodes if node["depth"] == last_depth)
    first["node_type"] = COMBAT
    last["node_type"] = REST

    residual: list[str] = []
    remaining_quota = dict(quota)
    remaining_quota[COMBAT] = remaining_quota.get(COMBAT, 0) - 1
    remaining_quota[REST] = remaining_quota.get(REST, 0) - 1
    for node_type, count in remaining_quota.items():
        residual.extend([node_type] * count)

    open_nodes = [node for node in nodes if node["node_type"] is None]
    if len(residual) != len(open_nodes):
        raise ValueError(
            f"node quota totals {len(residual) + 2} but the depth structure holds "
            f"{len(nodes)} nodes"
        )

    for _ in range(shuffle_attempts):
        candidate = list(residual)
        rng.shuffle(candidate)
        for node, node_type in zip(open_nodes, candidate):
            node["node_type"] = node_type
        if _placement_ok(nodes, last_depth):
            return _append_boss(nodes, last_depth)

    # On exhaustion, assign greedily. The doc says "in quota order"; that alone
    # cannot satisfy the 휴식-adjacency rule for this depth shape (the only
    # slots left when 휴식's turn comes are at depth 6, adjacent to the fixed
    # depth-7 rest), so constrained types are placed first and the unconstrained
    # remainder fills in quota order.
    for node in open_nodes:
        node["node_type"] = None
    pending = list(residual)
    for node_type in CONSTRAINED_TYPES:
        while node_type in pending:
            pending.remove(node_type)
            slot = _first_legal_slot(nodes, open_nodes, node_type, last_depth)
            slot["node_type"] = node_type
    for node in open_nodes:
        if node["node_type"] is None:
            node["node_type"] = pending.pop(0)
    return _append_boss(nodes, last_depth)


def _first_legal_slot(nodes: list[dict], open_nodes: list[dict], node_type: str,
                      last_depth: int) -> dict:
    for node in open_nodes:
        if node["node_type"] is not None:
            continue
        node["node_type"] = node_type
        legal = _placement_ok(nodes, last_depth, partial=True)
        node["node_type"] = None
        if legal:
            return node
    for node in open_nodes:
        if node["node_type"] is None:
            return node
    raise ValueError("no slot available for greedy assignment")


def _placement_ok(nodes: list[dict], last_depth: int, partial: bool = False) -> bool:
    rest_depths = set()
    for node in nodes:
        node_type = node["node_type"]
        if node_type is None:
            if partial:
                continue
            return False
        if node_type == SHOP and node["depth"] < 3:
            return False
        if node_type == REST:
            rest_depths.add(node["depth"])
    return not any(depth + 1 in rest_depths for depth in rest_depths)


def _append_boss(nodes: list[dict], last_depth: int) -> list[dict]:
    boss_index = max(node["node_index"] for node in nodes) + 1
    nodes.append({"node_index": boss_index, "depth": last_depth + 1,
                  "node_type": BOSS})
    return nodes


# =====================================================================
# Step 4 — FALLBACK_TEMPLATE
# =====================================================================
def _fallback_template(depth_structure: list[int], quota: dict[str, int],
                       shuffle_attempts: int) -> GeneratedMap:
    """A hardcoded, pre-validated graph. Deterministic, no rng involved."""
    import random

    nodes: list[dict] = []
    by_depth: list[list[int]] = []
    index = 0
    for depth, count in enumerate(depth_structure, start=1):
        row = []
        for _ in range(count):
            nodes.append({"node_index": index, "depth": depth, "node_type": None})
            row.append(index)
            index += 1
        by_depth.append(row)

    edges: list[tuple[int, int]] = []
    for d in range(len(depth_structure) - 1):
        source_row, target_row = by_depth[d], by_depth[d + 1]
        for i, source in enumerate(source_row):
            a, b = len(source_row), len(target_row)
            if a == 1:
                span = range(b)
            else:
                lo = (i * (b - 1)) // (a - 1)
                hi = max(lo, min(((i + 1) * (b - 1)) // (a - 1), b - 1))
                if lo == hi and d + 1 not in (5, 6):
                    lo = max(0, lo - 1)
                span = range(lo, hi + 1)
            for j in span:
                edges.append((source, target_row[j]))

    assigned = _assign_node_types(random.Random(0), nodes, depth_structure, quota,
                                 shuffle_attempts)
    generated = GeneratedMap(nodes=assigned, edges=edges, used_fallback=True)
    generated.boss_index = len(assigned) - 1
    return generated


# =====================================================================
# Step 6 — persist
# =====================================================================
def persist_map(db: Database, run_id: int, generated: GeneratedMap) -> None:
    """Store every node and every edge (§18.3). The seed lives on `runs`."""
    boss_index = generated.boss_index
    with db.tx() as conn:
        for node in generated.nodes:
            conn.execute(
                "INSERT INTO run_nodes (run_id, node_index, depth, node_type, state) "
                "VALUES (?, ?, ?, ?, 'available')",
                (run_id, node["node_index"], node["depth"], node["node_type"]),
            )
        for source, target in generated.edges:
            conn.execute(
                "INSERT OR IGNORE INTO run_edges (run_id, from_node_index, "
                "to_node_index) VALUES (?, ?, ?)",
                (run_id, source, target),
            )
        # Terminal depth converges on the boss node.
        terminal_depth = max(
            node["depth"] for node in generated.nodes if node["node_index"] != boss_index
        )
        for node in generated.nodes:
            if node["depth"] == terminal_depth and node["node_index"] != boss_index:
                conn.execute(
                    "INSERT OR IGNORE INTO run_edges (run_id, from_node_index, "
                    "to_node_index) VALUES (?, ?, ?)",
                    (run_id, node["node_index"], boss_index),
                )


def available_next_nodes(db: Database, run_id: int,
                         current_node_index: int | None) -> list[dict]:
    """The branch options the map screen offers (§19.3)."""
    if current_node_index is None:
        return [dict(row) for row in db.query(
            "SELECT * FROM run_nodes WHERE run_id = ? AND depth = 1 "
            "ORDER BY node_index", (run_id,))]
    return [dict(row) for row in db.query(
        "SELECT n.* FROM run_edges e JOIN run_nodes n ON n.run_id = e.run_id "
        "AND n.node_index = e.to_node_index WHERE e.run_id = ? "
        "AND e.from_node_index = ? ORDER BY n.node_index",
        (run_id, current_node_index))]
