"""맵 화면 렌더링 (설계 문서 §11 "맵 화면 — 필수, 갱신마다 단일 이미지").

§3 반영: 층별 분기 구조를 그대로 그리고, 지금 선택 가능한 노드를 강조한다.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..game.mapgen import GameMap
from . import base
from .base import BG_DARK, BG_PANEL, FG, FG_MUTED, GOLD, NODE_COLORS

WIDTH = 900
PAD = 28
FLOOR_GAP = 78
NODE_R = 22

# 노드 아이콘은 한글 한 글자로 표기한다. 번들 폰트에 이모지 글리프가 없어
# 이모지를 쓰면 이미지에서 두부(□)로 깨진다.
NODE_LABEL = {
    "combat": ("전", "전투"),
    "reward": ("보", "보상"),
    "rest": ("휴", "휴식"),
    "shop": ("상", "상점"),
    "event": ("?", "이벤트"),
    "boss": ("왕", "보스"),
}


def _positions(game_map: GameMap) -> dict[str, tuple[int, int]]:
    pos: dict[str, tuple[int, int]] = {}
    floors = game_map.floors
    inner_w = WIDTH - PAD * 2
    for depth, row in enumerate(floors):
        # 아래에서 위로 진행 — 마지막 층(보스)이 맨 위에 온다.
        y = PAD + 40 + (len(floors) - 1 - depth) * FLOOR_GAP
        for i, nid in enumerate(row):
            x = PAD + int(inner_w * (i + 1) / (len(row) + 1))
            pos[nid] = (x, y)
    return pos


def render_map(
    game_map: GameMap,
    current_node_id: str | None,
    cleared_ids: list[str],
    available_ids: list[str],
    title: str = "탐험 맵",
) -> bytes:
    pos = _positions(game_map)
    height = PAD * 2 + 40 + len(game_map.floors) * FLOOR_GAP

    img = Image.new("RGBA", (WIDTH, height), BG_DARK)
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD - 6), title, font=base.font(20, bold=True), fill=FG)
    legend = "   ".join(f"{ico}={name}" for ico, name in NODE_LABEL.values())
    draw.text((PAD, PAD + 18), legend, font=base.font(12), fill=FG_MUTED)

    cleared = set(cleared_ids)
    available = set(available_ids)

    # ---- 간선 ----
    for nid, targets in game_map.edges.items():
        if nid not in pos:
            continue
        x0, y0 = pos[nid]
        for tid in targets:
            if tid not in pos:
                continue
            x1, y1 = pos[tid]
            on_path = nid == current_node_id or nid in cleared
            draw.line((x0, y0, x1, y1),
                      fill=GOLD if (nid == current_node_id and tid in available) else
                      ((92, 98, 120) if on_path else (56, 60, 76)),
                      width=3 if nid == current_node_id else 2)

    # ---- 노드 ----
    label_font = base.font(12)
    small = base.font(11)
    for nid, node in game_map.nodes.items():
        x, y = pos[nid]
        color = NODE_COLORS.get(node.type, (120, 120, 140))
        icon, name = NODE_LABEL.get(node.type, ("•", node.type))

        is_current = nid == current_node_id
        is_available = nid in available
        is_cleared = nid in cleared

        if is_available:
            draw.ellipse((x - NODE_R - 6, y - NODE_R - 6, x + NODE_R + 6, y + NODE_R + 6),
                         outline=GOLD, width=3)

        fill = color if (is_available or is_current) else BG_PANEL
        outline = GOLD if is_current else color
        draw.ellipse((x - NODE_R, y - NODE_R, x + NODE_R, y + NODE_R),
                     fill=fill, outline=outline, width=2)

        base.draw_center(draw, (x, y - 3), icon, base.font(16),
                         (20, 20, 26) if (is_available or is_current) else color)
        base.draw_center(draw, (x, y + NODE_R + 12), name, label_font,
                         FG if (is_available or is_current) else FG_MUTED)

        if is_cleared:
            # 클리어 표시 — 체크 기호는 폰트에 없어 작은 점으로 그린다.
            cx, cy = x + NODE_R - 5, y - NODE_R + 5
            draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=(140, 220, 160))

    # ---- 층 번호 ----
    for depth in range(len(game_map.floors)):
        y = PAD + 40 + (len(game_map.floors) - 1 - depth) * FLOOR_GAP
        draw.text((8, y - 7), f"{depth + 1}F", font=small, fill=(80, 84, 104))

    return base.to_png_bytes(img)
