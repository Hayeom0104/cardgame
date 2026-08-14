"""맵 화면 렌더링 (설계 문서 §11 "맵 화면 — 필수, 갱신마다 단일 이미지").

§3 반영: 층별 분기 구조를 그대로 그리고, 지금 선택 가능한 노드를 강조한다.

진행 방향은 **왼쪽 → 오른쪽**이다. 층 수가 많아지면 세로 배치는 디스코드에서
심하게 축소되어 읽기 어려워지므로, 가로로 눕혀 폭을 활용한다.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..game.mapgen import GameMap
from . import base
from .base import FG, FG_DIM, FG_MUTED, GOLD, GOLD_DIM, NODE_COLORS

PAD = 24
HEADER_H = 58
FOOTER_H = 18

COL_W = 96          # 층 간 가로 간격
ROW_H = 96          # 분기 간 세로 간격
NODE_R = 24
BOSS_R = 32

# 노드 아이콘은 한글 한 글자로 표기한다. 번들 폰트에 이모지 글리프가 없어
# 이모지를 쓰면 이미지에서 두부(□)로 깨진다.
NODE_LABEL = {
    "combat": ("전", "전투"),
    "reward": ("보", "보상"),
    "rest": ("휴", "휴식"),
    "shop": ("상", "상점"),
    "event": ("?", "이벤트"),
    "boss": ("王", "보스"),
}


def _radius(node_type: str) -> int:
    return BOSS_R if node_type == "boss" else NODE_R


def _positions(game_map: GameMap, origin: tuple[int, int], max_rows: int) -> dict[str, tuple[int, int]]:
    """층 = 열(x), 분기 = 행(y). 각 열은 세로 중앙 정렬한다."""
    ox, oy = origin
    center_y = oy + (max_rows - 1) * ROW_H / 2
    pos: dict[str, tuple[int, int]] = {}
    for depth, row in enumerate(game_map.floors):
        x = ox + depth * COL_W
        span = (len(row) - 1) * ROW_H
        top = center_y - span / 2
        for i, nid in enumerate(row):
            pos[nid] = (int(x), int(top + i * ROW_H))
    return pos


def _bezier(p0: tuple[int, int], p1: tuple[int, int], steps: int = 24) -> list[tuple[int, int]]:
    """가로 진행에 맞춘 S자 곡선. 직선보다 경로가 눈에 잘 따라온다."""
    (x0, y0), (x1, y1) = p0, p1
    cx = (x0 + x1) / 2
    c0 = (cx, y0)
    c1 = (cx, y1)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * x0 + 3 * mt**2 * t * c0[0] + 3 * mt * t**2 * c1[0] + t**3 * x1
        y = mt**3 * y0 + 3 * mt**2 * t * c0[1] + 3 * mt * t**2 * c1[1] + t**3 * y1
        pts.append((int(x), int(y)))
    return pts


def _draw_node(
    img: Image.Image,
    center: tuple[int, int],
    node_type: str,
    state: str,          # "available" | "current" | "cleared" | "locked"
) -> None:
    x, y = center
    r = _radius(node_type)
    hi, lo = NODE_COLORS.get(node_type, ((150, 156, 176), (80, 86, 104)))
    box = (x - r, y - r, x + r, y + r)
    draw = ImageDraw.Draw(img)

    if state in ("available", "current"):
        base.outer_glow(img, box, hi, radius=r, blur=13,
                        opacity=150 if state == "current" else 105, spread=3)

    if state == "locked":
        top, bottom = base.shade(lo, 0.55), (26, 28, 40)
        border = (62, 68, 90)
    elif state == "cleared":
        top, bottom = base.shade(lo, 0.8), base.shade(lo, 0.45)
        border = base.shade(hi, 0.75)
    else:
        top, bottom = hi, lo
        border = base.shade(hi, 1.15)

    disc = base.linear_gradient((r * 2, r * 2), top, bottom)
    disc.putalpha(base.rounded_mask((r * 2, r * 2), r))
    img.alpha_composite(disc, (x - r, y - r))
    draw.ellipse(box, outline=border, width=2)

    if state == "current":
        # 현재 위치 — 노드 타입 색은 그대로 두고 바깥에 흰/금 이중 링을 두른다.
        draw.ellipse((x - r - 3, y - r - 3, x + r + 3, y + r + 3), outline=(255, 252, 240), width=2)
        draw.ellipse((x - r - 7, y - r - 7, x + r + 7, y + r + 7), outline=GOLD, width=2)

    icon, _ = NODE_LABEL.get(node_type, ("·", node_type))
    icon_font = base.font(int(r * 0.85), bold=True)
    icon_color = (22, 22, 30) if state in ("available", "current") else (
        FG_DIM if state == "cleared" else FG_MUTED
    )
    base.draw_center(draw, (x, y - 1), icon, icon_font, icon_color)

    if state == "cleared":
        # 클리어 표시 — 체크 기호는 폰트에 없어 작은 점으로 그린다.
        cx, cy = x + int(r * 0.68), y - int(r * 0.68)
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=(126, 224, 150),
                     outline=(20, 24, 30), width=2)


def render_map(
    game_map: GameMap,
    current_node_id: str | None,
    cleared_ids: list[str],
    available_ids: list[str],
    title: str = "탐험 맵",
    party: list[dict] | None = None,
) -> bytes:
    floors = game_map.floors
    max_rows = max(len(r) for r in floors)

    width = PAD * 2 + (len(floors) - 1) * COL_W + BOSS_R * 2 + 40
    height = PAD * 2 + HEADER_H + (max_rows - 1) * ROW_H + NODE_R * 2 + 34 + FOOTER_H

    img = base.scene_background((width, height), tint=(40, 52, 84))
    draw = ImageDraw.Draw(img)

    cleared = set(cleared_ids)
    available = set(available_ids)

    def state_of(nid: str) -> str:
        if nid == current_node_id:
            return "current"
        if nid in available:
            return "available"
        if nid in cleared:
            return "cleared"
        return "locked"

    origin = (PAD + NODE_R + 12, PAD + HEADER_H + NODE_R)
    pos = _positions(game_map, origin, max_rows)

    # ---- 헤더 ----
    draw.text((PAD, PAD - 4), base.sanitize(title), font=base.font(21, bold=True), fill=FG)

    depth = game_map.nodes[current_node_id].floor + 1 if current_node_id else 0
    base.pill(img, (PAD, PAD + 26), f"{depth} / {len(floors)} 층", base.font(12),
              fg=GOLD, bg=(46, 38, 24), border=GOLD_DIM)

    if party:
        px = PAD + 96
        for member in party:
            ratio = member["hp"] / member["max_hp"] if member["max_hp"] else 0
            hi, _ = base.hp_colors(ratio)
            label = f"{member['name']} {member['hp']}/{member['max_hp']}"
            px += base.pill(img, (px, PAD + 26), label, base.font(12),
                            fg=hi, bg=(28, 32, 46), border=base.shade(hi, 0.5)) + 7

    # 범례
    lx = width - PAD
    legend_font = base.font(11)
    for node_type in reversed(list(NODE_LABEL)):
        icon, name = NODE_LABEL[node_type]
        hi, lo = NODE_COLORS[node_type]
        w = base.text_width(draw, name, legend_font) + 22
        lx -= w + 6
        draw.ellipse((lx, PAD + 1, lx + 12, PAD + 13), fill=hi)
        draw.text((lx + 17, PAD), name, font=legend_font, fill=FG_MUTED)

    # ---- 간선 ----
    for nid, targets in game_map.edges.items():
        if nid not in pos:
            continue
        for tid in targets:
            if tid not in pos:
                continue
            src_state = state_of(nid)
            live = src_state == "current" and tid in available
            walked = nid in cleared and tid in cleared

            if live:
                color, w = GOLD, 3
            elif walked:
                color, w = (128, 136, 168), 3
            else:
                color, w = (54, 60, 84), 2

            pts = _bezier(pos[nid], pos[tid])
            draw.line(pts, fill=color, width=w, joint="curve")

    # ---- 노드 ----
    label_font = base.font(12)
    for nid, node in game_map.nodes.items():
        if nid not in pos:
            continue
        x, y = pos[nid]
        state = state_of(nid)
        _draw_node(img, (x, y), node.type, state)

        _, name = NODE_LABEL.get(node.type, ("", node.type))
        r = _radius(node.type)
        base.draw_center(draw, (x, y + r + 13), name, label_font,
                         FG if state in ("available", "current") else FG_MUTED)

    # ---- 층 번호 ----
    small = base.font(10)
    baseline = height - PAD - 8
    for dpt in range(len(floors)):
        x = origin[0] + dpt * COL_W
        base.draw_center(draw, (x, baseline), f"{dpt + 1}F", small, (86, 92, 118))

    return base.to_png_bytes(img)
