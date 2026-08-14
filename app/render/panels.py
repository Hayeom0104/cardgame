"""§11 — Pillow로 그리는 모든 화면.

전투는 아군·적 패널 **두 장을 한 번에** 보낸다 (§1.3.7). 각 4 MiB, 4096×4096
이하.

크기와 색은 `config/10_화면.toml`, 그림 파일 규칙은 `config/11_에셋.toml` 이
정한다. 코드에는 수치가 없다.

에셋 대체: 그림이 없거나 깨졌으면 **희귀도 색 바탕에 이름**을 그린다. 화면이
실패하는 일은 없고, 대시보드가 빠진 그림을 표시한다.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageDraw

from app.central.client import (MAX_PNG_BYTES, MAX_PNG_DIMENSION,
                                validate_png_attachment)
from app.render import theme as theme_module
from app.render.assets import AssetLibrary

logger = logging.getLogger(__name__)


class Canvas:
    """화면 하나를 그리는 동안의 붓과 설정 한 벌.

    `theme`(색·크기)과 `assets`(그림)를 함께 들고 다녀서, 그리는 함수마다
    설정을 다시 읽지 않아도 되게 한다.
    """

    def __init__(self, size: tuple[int, int], theme=None, assets=None):
        self.theme = theme or theme_module.load()
        self.assets = assets or AssetLibrary(self.theme)
        self.image = Image.new("RGB", size, self.theme.color("color_background"))
        self.draw = ImageDraw.Draw(self.image)
        self.width, self.height = size
        self.pad = self.theme.int_("screen_padding")
        self.gap = self.theme.int_("tile_gap")
        self.radius = self.theme.int_("corner_radius")

    # -- 자주 쓰는 색 ---------------------------------------------------
    @property
    def text(self):
        return self.theme.color("color_text")

    @property
    def muted(self):
        return self.theme.color("color_muted")

    @property
    def accent(self):
        return self.theme.color("color_accent")

    @property
    def panel(self):
        return self.theme.color("color_panel")

    @property
    def border(self):
        return self.theme.color("color_border")

    # -- 그리기 ---------------------------------------------------------
    def font(self, role: str = "body"):
        return self.theme.font(role=role)

    def title(self, text: str, *, right: str = "") -> None:
        self.draw.text((self.pad, self.pad // 2), text,
                       font=self.font("title"), fill=self.accent)
        if right:
            font = self.font("body")
            width = self.draw.textlength(right, font=font)
            self.draw.text((self.width - self.pad - width, self.pad),
                           right, font=font, fill=self.accent)

    def tile(self, box: tuple[int, int, int, int], *, fill=None,
             outline=None, width: int = 1) -> None:
        self.draw.rounded_rectangle(box, radius=self.radius,
                                    fill=fill or self.panel,
                                    outline=outline or self.border, width=width)

    def label(self, position: tuple[int, int], text: str, *, role: str = "body",
              color=None) -> None:
        self.draw.text(position, text, font=self.font(role),
                       fill=color or self.text)

    def paste(self, art: Image.Image, position: tuple[int, int]) -> None:
        self.image.paste(art, position, art if art.mode == "RGBA" else None)

    def bar(self, x: int, y: int, width: int, height: int,
            current: int, maximum: int, block: int = 0) -> None:
        """체력 막대. 방어막은 따로 적는다 — 한 대 맞으면 얼마가 남는지
        읽을 수 있어야 한다."""
        self.draw.rounded_rectangle((x, y, x + width, y + height), radius=4,
                                    fill=self.border)
        ratio = max(0.0, min(1.0, current / maximum if maximum else 0.0))
        color = (self.theme.color("color_hp_full") if ratio > 0.35
                 else self.theme.color("color_hp_low"))
        if ratio > 0:
            self.draw.rounded_rectangle(
                (x, y, x + int(width * ratio), y + height), radius=4, fill=color)
        small = self.font("small")
        self.draw.text((x + width + 6, y - 2), f"{current}/{maximum}",
                       font=small, fill=self.text)
        if block:
            self.draw.text((x + width + 66, y - 2), f"방어 {block}", font=small,
                           fill=self.theme.color("color_block"))

    def finish(self, filename: str) -> "Attachment":
        return to_attachment(self.image, filename)


# =====================================================================
# 첨부 파일
# =====================================================================
@dataclass
class Attachment:
    filename: str
    data_b64: str
    width: int
    height: int
    decoded_size: int

    def validate(self) -> None:
        validate_png_attachment(
            data_b64=self.data_b64, filename=self.filename,
            width=self.width, height=self.height, decoded_size=self.decoded_size,
        )


def to_attachment(image: Image.Image, filename: str) -> Attachment:
    """§1.3.7 한계 안에서 PNG로 인코딩한다.

    `data_b64` 는 `data:` 접두사가 없는 표준 base64 다.
    """
    if image.width > MAX_PNG_DIMENSION or image.height > MAX_PNG_DIMENSION:
        image = image.copy()
        image.thumbnail((MAX_PNG_DIMENSION, MAX_PNG_DIMENSION))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    raw = buffer.getvalue()
    if len(raw) > MAX_PNG_BYTES:
        raise ValueError(f"rendered PNG is {len(raw)} bytes, over the 4 MiB limit")

    attachment = Attachment(
        filename=filename,
        data_b64=base64.b64encode(raw).decode("ascii"),
        width=image.width, height=image.height, decoded_size=len(raw),
    )
    attachment.validate()
    return attachment


# =====================================================================
# 카드 한 장
# =====================================================================
def render_card(card: dict, canvas: Canvas, *, size: tuple[int, int] | None = None,
                footer: str = "", dimmed: bool = False) -> Image.Image:
    """카드 한 장. 손패·상점·보상·덱 화면이 모두 이걸 쓴다.

    `card` 에서 읽는 것: card_id, name, cost, element, rarity_tier,
    upgrade_tier, category.
    """
    theme = canvas.theme
    size = size or theme.size("card_size")
    width, height = size
    element = card.get("element")

    image = Image.new("RGBA", size, (*theme.color("color_panel"), 255))
    draw = ImageDraw.Draw(image)

    # 그림 자리 — 카드 위쪽 60%
    art_height = int(height * 0.6)
    art = canvas.assets.art("card", str(card.get("card_id", "")),
                            label=str(card.get("name", "")),
                            rarity=card.get("rarity_tier"), element=element,
                            size=(width, art_height))
    image.paste(art, (0, 0), art)

    # 속성 색 테두리 — 어느 캐릭터가 낼 수 있는 카드인지 한눈에 보이게
    draw.rectangle([0, 0, width - 1, height - 1],
                   outline=theme.element_color(element), width=2)

    body = theme.font(role="body")
    small = theme.font(role="small")
    name = str(card.get("name", ""))
    draw.text((8, art_height + 6), name[:10], font=body, fill=theme.color("color_text"))

    cost = card.get("cost")
    if cost is not None:
        # 비용은 왼쪽 위 원 안에 — 카드가 겹쳐 있어도 보이는 자리다.
        draw.ellipse((6, 6, 34, 34), fill=(*theme.color("color_background"), 230),
                     outline=theme.color("color_accent"), width=2)
        text = str(cost)
        draw.text((20 - draw.textlength(text, font=body) / 2, 11), text,
                  font=body, fill=theme.color("color_accent"))

    line = footer or str(card.get("category", ""))
    if line:
        draw.text((8, height - 22), line[:16], font=small,
                  fill=theme.color("color_muted"))

    upgrade = int(card.get("upgrade_tier") or 0)
    if upgrade:
        # 아래 오른쪽. 그림 위(왼쪽 위)에 쓰면 카드를 작게 그렸을 때 이름과
        # 겹치고, 오른쪽 위는 덱 화면의 장수 뱃지 자리다.
        text = f"+{upgrade}"
        draw.text((width - draw.textlength(text, font=small) - 8, height - 22),
                  text, font=small, fill=theme.color("color_accent"))

    if dimmed:
        # 살 수 없는 카드는 어둡게 덮는다 — 목록에서 빼면 무엇이 있었는지
        # 알 수 없게 된다.
        overlay = Image.new("RGBA", size, (0, 0, 0, 150))
        image = Image.alpha_composite(image, overlay)
    return image


# =====================================================================
# 전투
# =====================================================================
def render_ally_panel(units: list[dict], *, resource: int, round_no: int,
                      canvas: Canvas | None = None) -> Image.Image:
    """아군 패널 — 배치, 체력, 방어막, 상태이상 (§11)."""
    canvas = canvas or Canvas(theme_module.load().size("panel_size"))
    canvas.title(f"라운드 {round_no}", right=f"자원 {resource}")

    portrait = canvas.theme.size("portrait_size")
    slot_height = (canvas.height - 56 - canvas.pad) // 3
    for index, unit in enumerate(units[:3]):
        top = 56 + index * slot_height
        canvas.tile((canvas.pad, top, canvas.width - canvas.pad,
                     top + slot_height - canvas.gap))

        art_size = min(portrait[0], slot_height - canvas.gap - 12)
        art = canvas.assets.art(
            "character", str(unit.get("character_id", unit.get("name", ""))),
            label=str(unit.get("name", "?")), rarity=unit.get("tier", 1),
            size=(art_size, art_size))
        canvas.paste(art, (canvas.pad + 8, top + 8))

        left = canvas.pad + art_size + 20
        canvas.label((left, top + 10), str(unit.get("name", "?")))
        canvas.bar(left, top + 38, 300, 14, unit.get("hp_current", 0),
                   unit.get("hp_max", 1), unit.get("block", 0))
        statuses = " ".join(f"{entry['status_id']}×{entry['stacks']}"
                            for entry in unit.get("statuses", []))
        if statuses:
            canvas.label((left, top + 58), statuses[:60], role="small",
                         color=canvas.muted)
        if not unit.get("is_alive", True):
            canvas.label((canvas.width - 100, top + 30), "전투불능",
                         color=canvas.theme.color("color_hp_low"))
    return canvas.image


def render_enemy_panel(units: list[dict], telegraphs: dict[int, dict],
                       canvas: Canvas | None = None) -> Image.Image:
    """적 패널 — **모든** 적의 행동 예고를 포함한다 (§11).

    이미 행동한 적은 `행동 완료`를 보여준다. 빈칸도 아니고, 다음 라운드
    계획을 미리 지어내지도 않는다 (§2.8.6).
    """
    canvas = canvas or Canvas(theme_module.load().size("panel_size"))
    canvas.title("적")

    columns = 4
    cell_width = (canvas.width - canvas.pad * 2 - canvas.gap * (columns - 1)) // columns
    cell_height = (canvas.height - 48 - canvas.pad - canvas.gap) // 2
    for index, unit in enumerate(units[:8]):
        column, row = index % columns, index // columns
        left = canvas.pad + column * (cell_width + canvas.gap)
        top = 48 + row * (cell_height + canvas.gap)
        canvas.tile((left, top, left + cell_width, top + cell_height))

        art_size = min(60, cell_height - 60)
        art = canvas.assets.art(
            "enemy", str(unit.get("enemy_id", unit.get("name", ""))),
            label=str(unit.get("name", "?")), rarity=unit.get("tier", 1),
            size=(art_size, art_size))
        canvas.paste(art, (left + 8, top + 8))

        canvas.label((left + art_size + 16, top + 10),
                     str(unit.get("name", "?"))[:8], role="small")
        canvas.bar(left + 8, top + art_size + 16, 80, 10,
                   unit.get("hp_current", 0), unit.get("hp_max", 1),
                   unit.get("block", 0))

        telegraph = telegraphs.get(unit.get("battle_unit_id"), {})
        label = telegraph.get("label", "행동 완료")
        canvas.label((left + 8, top + cell_height - 24), f"▶ {label}"[:22],
                     role="small",
                     color=canvas.accent if telegraph.get("state") == "planned"
                     else canvas.muted)
        if not unit.get("is_alive", True):
            canvas.label((left + cell_width - 44, top + 30), "격파", role="small",
                         color=canvas.theme.color("color_hp_low"))
    return canvas.image


def render_battle_screen(ally_units: list[dict], enemy_units: list[dict],
                         telegraphs: dict[int, dict], *, resource: int,
                         round_no: int, hand: list[dict] | None = None
                         ) -> list[Attachment]:
    """§1.3.7 — 중앙봇은 한 action 에 PNG 두 장까지만 받는다.

    손패를 세 번째 첨부로 따로 보내면 실제 전투마다(손패가 항상 있으므로)
    한도를 넘겨 거절당했다 — 상태는 이미 `battle` 로 넘어갔는데 화면은
    지도에 멈춰 있던 원인 중 하나였다. 정보를 잃지 않도록 손패를 아군
    패널 아래에 이어 붙여 한 장으로 만든다."""
    theme = theme_module.load()
    assets = AssetLibrary(theme)
    size = theme.size("panel_size")

    ally_image = render_ally_panel(
        ally_units, resource=resource, round_no=round_no,
        canvas=Canvas(size, theme, assets))
    if hand:
        hand_image = render_hand(
            hand, resource=resource, canvas=Canvas(size, theme, assets))
        combined = Image.new("RGB", (size[0], size[1] * 2),
                             theme.color("color_background"))
        combined.paste(ally_image, (0, 0))
        combined.paste(hand_image, (0, size[1]))
        ally_image = combined

    return [
        to_attachment(ally_image, "deckout_ally.png"),
        to_attachment(render_enemy_panel(
            enemy_units, telegraphs, canvas=Canvas(size, theme, assets)),
            "deckout_enemy.png"),
    ]


def render_hand(hand: list[dict], *, resource: int,
                canvas: Canvas | None = None) -> Image.Image:
    """손패 — 낼 수 없는 카드는 어둡게, 목록에서 빼지는 않는다."""
    canvas = canvas or Canvas(theme_module.load().size("panel_size"))
    canvas.title("손패", right=f"자원 {resource}")

    card_size = canvas.theme.size("card_size")
    scale = min(1.0, (canvas.height - 70) / card_size[1])
    size = (int(card_size[0] * scale), int(card_size[1] * scale))
    for index, card in enumerate(hand[:6]):
        left = canvas.pad + index * (size[0] + canvas.gap)
        if left + size[0] > canvas.width:
            break
        affordable = int(card.get("cost", 0)) <= resource
        art = render_card(card, canvas, size=size, dimmed=not affordable)
        canvas.paste(art, (left, 56))
    return canvas.image


# =====================================================================
# 지도
# =====================================================================
def render_map(nodes: list[dict], edges: list[tuple[int, int]], *,
               current_node_index: int | None,
               available: set[int] | None = None,
               visited: set[int] | None = None,
               world_id: str | None = None) -> Attachment:
    """맵 화면 — 지도 전체를 한 장으로 (§11).

    칸은 네 가지로 그려진다: 지금 서 있는 칸, 갈 수 있는 칸, 지나온 칸,
    그리고 아직 닿지 않은 칸. 지나온 칸과 닿지 않은 칸을 같은 색으로 그리면
    지도가 어디까지 왔는지 말해 주지 못한다.
    """
    canvas = Canvas(theme_module.load().size("map_size"))
    available = available or set()
    visited = visited or set()

    if world_id:
        # 월드 그림을 넣어 두었으면 바탕으로 깐다. 없으면 그냥 배경색이다.
        backdrop = canvas.assets.load("world", world_id,
                                      (canvas.width, canvas.height))
        if backdrop is not None:
            canvas.image.paste(backdrop.convert("RGB"), (0, 0))
            veil = Image.new("RGBA", (canvas.width, canvas.height),
                             (*canvas.theme.color("color_background"), 190))
            canvas.image.paste(veil, (0, 0), veil)
            canvas.draw = ImageDraw.Draw(canvas.image)

    by_depth: dict[int, list[dict]] = {}
    for node in nodes:
        by_depth.setdefault(node["depth"], []).append(node)
    max_depth = max(by_depth) if by_depth else 1

    positions: dict[int, tuple[int, int]] = {}
    for depth, row in sorted(by_depth.items()):
        y = 48 + int((depth - 1) * (canvas.height - 110) / max(1, max_depth - 1))
        for index, node in enumerate(sorted(row, key=lambda n: n["node_index"])):
            x = int(canvas.width * (index + 1) / (len(row) + 1))
            positions[node["node_index"]] = (x, y)

    for source, target in edges:
        if source in positions and target in positions:
            canvas.draw.line((*positions[source], *positions[target]),
                             fill=canvas.border, width=2)

    visited_color = canvas.theme.color("color_visited")
    selectable = canvas.theme.color("color_selectable")
    for node in nodes:
        x, y = positions[node["node_index"]]
        is_current = node["node_index"] == current_node_index
        is_open = node["node_index"] in available
        is_past = node["node_index"] in visited
        if is_current:
            fill = canvas.accent
        elif is_open:
            fill = canvas.theme.node_color(node["node_type"])
        elif is_past:
            fill = visited_color
        else:
            fill = canvas.muted
        canvas.draw.ellipse((x - 22, y - 22, x + 22, y + 22), fill=fill,
                            outline=selectable if is_open else canvas.border,
                            width=3 if is_open else 1)
        text = str(node["node_type"])[:2]
        font = canvas.font("small")
        offset = canvas.draw.textlength(text, font=font) / 2
        canvas.draw.text((x - offset, y - 8), text, font=font, fill=canvas.text)
    return canvas.finish("deckout_map.png")


# =====================================================================
# 상점
# =====================================================================
def render_shop(items: list[dict], *, currency: int, title: str = "상점",
                currency_label: str = "탐험 자금") -> Attachment:
    """상점 화면 — 진열된 물건, 가격, 살 수 있는지 여부 (§7).

    이미 산 물건은 목록에서 빼지 않고 `판매 완료` 로 덮는다. 무엇이 있었는지
    알 수 없게 되면 다시 왔을 때 헷갈린다.
    """
    canvas = Canvas(theme_module.load().size("shop_size"))
    canvas.title(title, right=f"{currency_label} {currency}")

    card_size = canvas.theme.size("card_size")
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (card_size[0] + canvas.gap))
    for index, item in enumerate(items):
        column, row = index % columns, index // columns
        left = canvas.pad + column * (card_size[0] + canvas.gap)
        top = 60 + row * (card_size[1] + canvas.gap + 26)
        if top + card_size[1] > canvas.height:
            break

        price = int(item.get("price", 0))
        purchased = bool(item.get("purchased"))
        affordable = price <= currency and not purchased

        if item.get("kind") == "card":
            art = render_card(item, canvas, size=card_size, dimmed=not affordable)
        else:
            art = _effect_tile(item, canvas, card_size, dimmed=not affordable)
        canvas.paste(art, (left, top))

        if purchased:
            # 물건 이름 위에 그냥 겹쳐 쓰면 두 글자가 뒤엉켜 둘 다 못 읽는다.
            # 띠를 깔고 그 위에 쓴다.
            band_top = top + card_size[1] // 2 - 16
            canvas.draw.rectangle(
                (left, band_top, left + card_size[0], band_top + 32),
                fill=canvas.theme.color("color_sold_out"))
            text, font = "판매 완료", canvas.font("body")
            offset = (card_size[0] - canvas.draw.textlength(text, font=font)) / 2
            canvas.draw.text((left + offset, band_top + 6), text, font=font,
                             fill=canvas.text)
        canvas.label((left + 4, top + card_size[1] + 4), f"{price}",
                     color=canvas.accent if affordable else canvas.muted)
    return canvas.finish("deckout_shop.png")


def _effect_tile(item: dict, canvas: Canvas, size: tuple[int, int],
                 *, dimmed: bool) -> Image.Image:
    """카드가 아닌 상점 물건 (즉시 회복, 저주 제거 …)."""
    theme = canvas.theme
    image = Image.new("RGBA", size, (*theme.color("color_panel"), 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1],
                   outline=theme.color("color_border"), width=2)
    name = str(item.get("name", ""))
    font = theme.font(role="body")
    y = size[1] // 2 - 20
    for line in _wrap(draw, name, font, size[0] - 16):
        draw.text((8, y), line, font=font, fill=theme.color("color_text"))
        y += font.size + 4 if hasattr(font, "size") else 18
    if dimmed:
        image = Image.alpha_composite(image, Image.new("RGBA", size, (0, 0, 0, 150)))
    return image


# =====================================================================
# 뽑기
# =====================================================================
def render_banner(banner: dict, *, rates: dict, pity: dict | None = None,
                  carta: int = 0) -> Attachment:
    """배너 화면 — 픽업 대상, 확률, 천장까지 남은 횟수 (§5).

    확률과 천장은 반드시 보여준다. 플레이어가 무엇에 돈을 쓰는지 알 수
    없으면 안 된다.
    """
    canvas = Canvas(theme_module.load().size("banner_size"))
    banner_id = str(banner.get("banner_id", ""))

    art = canvas.assets.art("banner", banner_id,
                            label=str(banner.get("name", banner_id)),
                            size=(canvas.width, canvas.theme.size("banner_size")[1] // 2))
    canvas.paste(art, (0, 0))
    canvas.draw = ImageDraw.Draw(canvas.image)

    top = art.height + canvas.gap
    canvas.label((canvas.pad, top), str(banner.get("name", banner_id)),
                 role="title", color=canvas.accent)
    right = f"카르타 {carta}"
    width = canvas.draw.textlength(right, font=canvas.font("body"))
    canvas.label((canvas.width - canvas.pad - width, top + 6), right,
                 color=canvas.accent)

    top += 40
    pickup = banner.get("pickup_name") or banner.get("pickup_target_id")
    if pickup:
        canvas.label((canvas.pad, top), f"픽업  {pickup}")
        top += 26

    line = "  ".join(f"{name} {float(value) * 100:.2f}%"
                     for name, value in rates.items())
    canvas.label((canvas.pad, top), line, role="small", color=canvas.muted)
    top += 24

    if pity:
        remaining = pity.get("until_hard")
        text = f"확정까지 {remaining}회" if remaining is not None else ""
        if pity.get("guarantee_pending"):
            text += "   다음 최고 등급은 픽업 확정"
        if text:
            canvas.label((canvas.pad, top), text, role="small", color=canvas.accent)
    return canvas.finish("deckout_banner.png")


def render_gacha_results(results: list[dict]) -> Attachment:
    """뽑기 결과 — 뽑은 것 전부를 한 장에 (§5.9)."""
    canvas = Canvas(theme_module.load().size("gacha_result_size"))
    canvas.title(f"뽑기 결과 {len(results)}회")

    card_size = canvas.theme.size("card_size")
    scale = 0.8
    size = (int(card_size[0] * scale), int(card_size[1] * scale))
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (size[0] + canvas.gap))
    for index, result in enumerate(results):
        column, row = index % columns, index // columns
        left = canvas.pad + column * (size[0] + canvas.gap)
        top = 56 + row * (size[1] + canvas.gap + 20)
        if top + size[1] > canvas.height:
            break

        kind = result.get("kind")
        kind = kind if kind in ("character", "passive") else "card"
        art = canvas.assets.art(
            kind, str(result.get("entity_id", "")),
            label=str(result.get("name", result.get("entity_id", ""))),
            rarity=result.get("rarity_tier") or result.get("star_rank"),
            size=size)
        canvas.paste(art, (left, top))

        note = ""
        if result.get("is_duplicate"):
            note = f"중복 · 조각 {result.get('fragments', 0)}"
        elif result.get("forced"):
            note = "확정"
        if note:
            canvas.label((left + 2, top + size[1] + 2), note[:18], role="small",
                         color=canvas.accent)
    return canvas.finish("deckout_gacha.png")


# =====================================================================
# 준비 화면
# =====================================================================
def render_prep(party: list[dict], *, world: str, deck: list[dict] | None = None,
                passives: list[dict] | None = None) -> Attachment:
    """준비 화면 — 런에 얼려질 빌드를 그대로 보여준다 (§16.2.3).

    여기 보이는 값이 런 시작 시점에 고정되는 바로 그 값이다.
    """
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title("준비", right=world)

    portrait = canvas.theme.size("portrait_size")
    for index, member in enumerate(party[:3]):
        left = canvas.pad + index * (portrait[0] + canvas.gap + 150)
        art = canvas.assets.art("character", str(member.get("character_id", "")),
                                label=str(member.get("name", "")),
                                rarity=member.get("star_rank"), size=portrait)
        canvas.paste(art, (left, 56))

        text_left = left + portrait[0] + 10
        star = "★" * int(member.get("star_rank", 1))
        canvas.label((text_left, 60), f"{member.get('name', '')} {star}")
        canvas.label((text_left, 84),
                     f"{member.get('element', '')} · {member.get('job_role', '')}",
                     role="small", color=canvas.muted)
        for row, (key, name) in enumerate(
                (("hp", "HP"), ("atk", "공"), ("def", "방"), ("spd", "속"))):
            canvas.label((text_left, 106 + row * 18),
                         f"{name} {member.get(key, 0)}", role="small")

    top = 56 + portrait[1] + canvas.gap * 2
    if deck:
        canvas.label((canvas.pad, top), f"덱 {len(deck)}장", color=canvas.accent)
        top += 26
        card_size = canvas.theme.size("card_size")
        scale = 0.55
        size = (int(card_size[0] * scale), int(card_size[1] * scale))
        for index, card in enumerate(deck):
            left = canvas.pad + index * (size[0] + 6)
            if left + size[0] > canvas.width - canvas.pad:
                canvas.label((left, top + size[1] // 2),
                             f"+{len(deck) - index}", role="small",
                             color=canvas.muted)
                break
            canvas.paste(render_card(card, canvas, size=size), (left, top))
        top += size[1] + canvas.gap

    if passives:
        names = ", ".join(str(entry.get("name", "")) for entry in passives)
        canvas.label((canvas.pad, top), f"패시브  {names}"[:70], role="small",
                     color=canvas.muted)
    return canvas.finish("deckout_prep.png")


def render_deck(cards: list[dict], *, character: dict, title: str,
                total: int) -> Attachment:
    """덱 구성 화면 — 지금 고른 대로라면 덱이 어떻게 되는지 (§3.2).

    같은 카드가 여러 장이면 한 장만 그리고 오른쪽 위에 장수를 적는다. 18장을
    낱장으로 늘어놓으면 무엇이 몇 장인지가 오히려 안 보인다.
    """
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title(title, right=f"{total}장")

    portrait = canvas.theme.size("portrait_size")
    if character:
        art = canvas.assets.art("character", str(character.get("card_id", "")),
                                label=str(character.get("name", "")),
                                rarity=character.get("rarity_tier"),
                                size=portrait)
        canvas.paste(art, (canvas.pad, 56))

    card_size = canvas.theme.size("card_size")
    scale = 0.62
    size = (int(card_size[0] * scale), int(card_size[1] * scale))
    left_edge = canvas.pad + portrait[0] + canvas.gap
    columns = max(1, (canvas.width - left_edge - canvas.pad + canvas.gap)
                  // (size[0] + canvas.gap))

    ordered = sorted(cards, key=lambda entry: (-int(entry.get("count", 1)),
                                               str(entry.get("name", ""))))
    for index, card in enumerate(ordered):
        column, row = index % columns, index // columns
        left = left_edge + column * (size[0] + canvas.gap)
        top = 56 + row * (size[1] + canvas.gap)
        if top + size[1] > canvas.height - canvas.pad:
            break
        canvas.paste(render_card(card, canvas, size=size), (left, top))

        count = int(card.get("count", 1))
        if count > 1:
            text = f"×{count}"
            font = canvas.font("body")
            width = canvas.draw.textlength(text, font=font)
            badge_left = left + size[0] - width - 12
            canvas.draw.rounded_rectangle(
                (badge_left - 4, top + 4, left + size[0] - 4, top + 26),
                radius=6, fill=canvas.theme.color("color_background"))
            canvas.draw.text((badge_left, top + 6), text, font=font,
                             fill=canvas.accent)
    return canvas.finish("deckout_deck.png")


def render_collection(cards: list[dict], *, title: str) -> Attachment:
    """소장 목록 — 캐릭터 카드를 앞에 두고 행동 카드를 뒤에 잇는다.

    캐릭터도 카드이므로 같은 화면에 같은 모양으로 놓는다. 다만 파티 자리를
    차지하는 쪽이라 먼저 보여준다.
    """
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title(title)

    card_size = canvas.theme.size("card_size")
    scale = 0.55
    size = (int(card_size[0] * scale), int(card_size[1] * scale))
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (size[0] + canvas.gap))
    rows = max(1, (canvas.height - 56 - canvas.pad + canvas.gap)
               // (size[1] + canvas.gap))
    capacity = columns * rows

    ordered = sorted(cards, key=lambda entry: (entry.get("kind") != "character",
                                               str(entry.get("name", ""))))
    for index, card in enumerate(ordered[:capacity]):
        column, row = index % columns, index // columns
        left = canvas.pad + column * (size[0] + canvas.gap)
        top = 56 + row * (size[1] + canvas.gap)
        kind = card.get("kind")
        kind = kind if kind in ("character", "passive") else "card"
        if kind != "card":
            # 캐릭터와 패시브는 그림을 각자의 폴더에서 찾는다. 행동 카드처럼
            # 코스트나 원소를 그릴 것이 없어서 그림 한 장으로 놓는다.
            art = canvas.assets.art(kind, str(card.get("card_id", "")),
                                    label=str(card.get("name", "")),
                                    rarity=card.get("rarity_tier"), size=size)
            canvas.paste(art, (left, top))
            canvas.draw.rectangle(
                [left, top, left + size[0] - 1, top + size[1] - 1],
                outline=canvas.accent, width=2)
        else:
            canvas.paste(render_card(card, canvas, size=size), (left, top))

    hidden = len(ordered) - capacity
    if hidden > 0:
        canvas.label((canvas.pad, canvas.height - 24), f"그 외 {hidden}장",
                     role="small", color=canvas.muted)
    return canvas.finish("deckout_collection.png")


# =====================================================================
# 정산
# =====================================================================
def render_settlement(report: dict) -> Attachment:
    """런 인벤토리 / 정산 화면 — 무엇을 지키고 무엇을 잃었는지 (§8.6.3, §11)."""
    canvas = Canvas(theme_module.load().size("panel_size"))
    canvas.title("정산")

    inventory = report.get("inventory", {})
    rows = [
        ("보관", inventory.get("kept", []), canvas.theme.color("color_hp_full")),
        ("등급 하락", inventory.get("tiered_down", []), canvas.accent),
        ("소실", inventory.get("lost", []), canvas.theme.color("color_hp_low")),
        ("파괴", inventory.get("destroyed_by_tier_down", []), canvas.muted),
    ]
    y = 56
    for label, entries, color in rows:
        canvas.label((canvas.pad, y), f"{label} {len(entries)}", color=color)
        for entry in entries[:4]:
            y += 20
            name = (entry.get("equipment_def_id")
                    or f"강화석 T{entry.get('stone_tier')}")
            canvas.label((canvas.pad + 20, y),
                         f"· {name} (T{entry.get('tier', 0)})", role="small",
                         color=canvas.muted)
        y += 28

    rewards = report.get("rewards") or {}
    canvas.label((canvas.width - 240, 56),
                 f"코인 {rewards.get('coin', 0)} · "
                 f"카르타 {rewards.get('carta', 0)}", role="small")
    return canvas.finish("deckout_settlement.png")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        trial = current + character
        if draw.textlength(trial, font=font) > width and current:
            lines.append(current)
            current = character
        else:
            current = trial
    if current:
        lines.append(current)
    return lines[:4]
