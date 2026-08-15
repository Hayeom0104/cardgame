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
from app.render import kit
from app.render.assets import AssetLibrary

logger = logging.getLogger(__name__)


class Canvas:
    """화면 하나를 그리는 동안의 붓과 설정 한 벌.

    `theme`(색·크기)과 `assets`(그림)를 함께 들고 다녀서, 그리는 함수마다
    설정을 다시 읽지 않아도 되게 한다.

    모든 화면이 이 클래스를 거쳐 그려지므로, 표면 표현(그라데이션·그림자·
    발광)은 여기 한 곳에서 준다. 개별 화면은 배치만 신경 쓰면 된다.
    """

    def __init__(self, size: tuple[int, int], theme=None, assets=None,
                 *, tint: str = "color_tint_hub"):
        self.theme = theme or theme_module.load()
        self.assets = assets or AssetLibrary(self.theme)
        self.width, self.height = size
        self.pad = self.theme.int_("screen_padding")
        self.gap = self.theme.int_("tile_gap")
        self.radius = self.theme.int_("corner_radius")

        # 평면 단색 대신 그라데이션 + 광원 + 비네트로 바탕을 깐다.
        self.image = kit.background(
            size, self.theme.color("color_background"), self.theme.color(tint),
            glow=float(self.theme.get("background_glow_strength", 0.42)),
            veil=float(self.theme.get("background_vignette_strength", 0.55)),
        )
        self.draw = ImageDraw.Draw(self.image)

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
    def panel_top(self):
        return self.theme.color("color_panel_top")

    @property
    def border(self):
        return self.theme.color("color_border")

    def hp_colors(self, ratio: float) -> tuple[tuple, tuple]:
        """체력 비율에 따른 게이지 색 한 쌍(밝은 쪽, 어두운 쪽).

        가득/다침/위험 세 단계로 나눈다 — 두 단계뿐이면 "위험한가"는 알아도
        "얼마나 여유가 있는가"는 읽히지 않는다.
        """
        if ratio > 0.5:
            return (self.theme.color("color_hp_full"),
                    self.theme.color("color_hp_full_low"))
        if ratio > 0.25:
            return (self.theme.color("color_hp_hurt"),
                    self.theme.color("color_hp_hurt_low"))
        return (self.theme.color("color_hp_crit"),
                self.theme.color("color_hp_crit_low"))

    # -- 그리기 ---------------------------------------------------------
    def font(self, role: str = "body"):
        return self.theme.font(role=role)

    def title(self, text: str, *, right: str = "", subtitle: str = "") -> None:
        self.draw.text((self.pad, self.pad // 2), kit.sanitize(text),
                       font=self.font("title"), fill=self.text)
        if subtitle:
            self.draw.text((self.pad, self.pad // 2 + self.theme.int_("font_size_title") + 2),
                           kit.sanitize(subtitle), font=self.font("small"), fill=self.muted)
        if right:
            font = self.font("body")
            right = kit.sanitize(right)
            width = self.draw.textlength(right, font=font)
            self.draw.text((self.width - self.pad - width, self.pad),
                           right, font=font, fill=self.accent)

    def tile(self, box: tuple[int, int, int, int], *, fill=None,
             outline=None, width: int = 1, glow=None, dimmed: bool = False) -> None:
        """칸 하나. 그라데이션·그림자·상단 하이라이트가 함께 들어간다.

        `glow` 색을 주면 둘레에 빛이 번져 "지금 여기"를 가리킨다.
        """
        if glow is not None:
            kit.outer_glow(self.image, box, glow, radius=self.radius,
                           opacity=self.theme.int_("glow_opacity_active"), spread=4)
        if dimmed:
            top = self.theme.color("color_disabled_top")
            bottom = self.theme.color("color_disabled")
            outline = outline or self.theme.color("color_disabled_border")
        else:
            top = fill or self.panel_top
            bottom = fill or self.panel
        kit.panel(self.image, box, top=top, bottom=bottom, radius=self.radius,
                  border=outline or self.border, border_width=max(1, width),
                  shadow=not dimmed)

    def label(self, position: tuple[int, int], text: str, *, role: str = "body",
              color=None) -> None:
        self.draw.text(position, kit.sanitize(text), font=self.font(role),
                       fill=color or self.text)

    def pill(self, position: tuple[int, int], text: str, *, role: str = "small",
             color=None, back=None, border=None) -> int:
        """작은 배지. 반환값이 폭이라 옆으로 이어 붙이기 쉽다."""
        return kit.pill(self.image, position, text, self.font(role),
                        fg=color or self.text,
                        bg=back or self.theme.color("color_gauge_back"),
                        border=border)

    def paste(self, art: Image.Image, position: tuple[int, int]) -> None:
        if art.mode == "RGBA":
            self.image.alpha_composite(art, position)
        else:
            self.image.paste(art, position)

    def art_tile(self, box: tuple[int, int, int, int], art: Image.Image | None, *,
                 grayscale: bool = False, fade_bottom: bool = False,
                 fade_top: bool = False, outline=None) -> None:
        """그림을 둥근 틀에 넣어 붙이고 테두리를 두른다."""
        kit.framed_art(self.image, box, art, radius=max(1, self.radius - 2),
                       grayscale=grayscale, fade_bottom=fade_bottom, fade_top=fade_top)
        if outline:
            self.draw.rounded_rectangle(box, radius=max(1, self.radius - 2),
                                        outline=outline, width=2)

    def bar(self, x: int, y: int, width: int, height: int,
            current: int, maximum: int, block: int = 0) -> None:
        """체력 막대. 방어막은 따로 적는다 — 한 대 맞으면 얼마가 남는지
        읽을 수 있어야 한다."""
        ratio = max(0.0, min(1.0, current / maximum if maximum else 0.0))
        high, low = self.hp_colors(ratio)
        kit.gauge(self.image, (x, y, x + width, y + height), ratio,
                  high=high, low=low, back=self.theme.color("color_gauge_back"))
        small = self.font("small")
        self.draw.text((x + width + 6, y - 2), f"{current}/{maximum}",
                       font=small, fill=self.text)
        if block:
            kit.pill(self.image, (x + width + 66, y - 4), f"방어 {block}", small,
                     fg=self.theme.color("color_block_text"),
                     bg=self.theme.color("color_block_back"),
                     border=self.theme.color("color_block"))

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

    if image.mode != "RGB":
        # 배경을 RGBA 로 합성해 그리므로 저장 직전에 평탄화한다. 알파를 남기면
        # 파일이 커져 4 MiB 한계에 불필요하게 가까워진다.
        image = image.convert("RGB")

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

    두 축을 색으로 나눠 보여준다 — **테두리는 등급**(가챠에서 얼마나 안
    나오는가), **안쪽 바탕은 종류**(공격/방어/버프/디버프/회복)다. 둘을 한
    색으로 합치면 "귀한 카드"와 "센 카드"가 구분되지 않는다.
    """
    theme = canvas.theme
    size = size or theme.size("card_size")
    width, height = size
    element = card.get("element")
    # 등급이 아예 없는 것(캐릭터 등)과 1등급 카드를 구분한다. 캐릭터는 등급이
    # 아니라 성급으로 표시되므로 보석을 그리면 안 된다.
    graded = card.get("rarity_tier") is not None
    rarity = int(card.get("rarity_tier") or 1)
    category = str(card.get("category", ""))

    kind_color = _card_kind_color(theme, category, element)
    border_color = _rarity_border(theme, rarity)
    radius = theme.int_("corner_radius")

    image = Image.new("RGBA", size, (0, 0, 0, 0))
    if dimmed:
        top = theme.color("color_disabled_top")
        bottom = theme.color("color_disabled")
        border_color = theme.color("color_disabled_border")
    else:
        top = kit.mix(theme.color("color_panel_top"), kind_color, 0.26)
        bottom = kit.mix(theme.color("color_panel"), kind_color, 0.10)
    kit.panel(image, (0, 0, width - 1, height - 1), top=top, bottom=bottom,
              radius=radius, border=border_color, border_width=2, shadow=False)

    draw = ImageDraw.Draw(image)
    body = theme.font(role="body")
    small = theme.font(role="small")

    # 그림 — 카드 안쪽을 거의 꽉 채운다. 이름·배지는 그림 위에 얹고, 위아래
    # 가장자리만 어둡게 깔아(fade) 글자가 묻히지 않게 한다.
    inset = 5
    # label 을 비워 둔다 — 이름은 이 함수가 위쪽에 직접 얹으므로, 그림에마저
    # 이름이 있으면 카드 아래쪽에서 글자가 겹친다.
    art = canvas.assets.art("card", str(card.get("card_id", "")), label="",
                            rarity=rarity, element=element,
                            size=(width - inset * 2, height - inset * 2))
    kit.framed_art(image, (inset, inset, width - inset, height - inset), art,
                   radius=max(1, radius - 2), grayscale=dimmed,
                   fade_top=True, fade_bottom=True)

    # 이름 — 비용 원과 겹치지 않게 폭을 미리 뺀다
    cost = card.get("cost")
    name_width = width - 20 - (30 if cost is not None else 0)
    draw.text((10, 9), kit.truncate(draw, str(card.get("name", "")), body, name_width),
              font=body, fill=theme.color("color_muted") if dimmed else theme.color("color_text"))

    if cost is not None:
        # 비용은 오른쪽 위 원 안에 — 카드를 부채꼴로 겹쳐도 보이는 자리다.
        cx = width - 22
        draw.ellipse((cx - 14, 6, cx + 14, 34),
                     fill=theme.color("color_background"),
                     outline=theme.color("color_disabled_border") if dimmed
                     else theme.color("color_accent"), width=2)
        text = str(cost)
        draw.text((cx - draw.textlength(text, font=body) / 2, 11), text, font=body,
                  fill=theme.color("color_muted") if dimmed else theme.color("color_accent"))

    # 종류 배지 — 그림 위에 얹어 세로 공간을 아낀다
    if category:
        badge_color = theme.color("color_disabled_border") if dimmed else kind_color
        kit.pill(image, (14, height - 48), category[:6], small,
                 fg=theme.color("color_muted") if dimmed else theme.color("color_text"),
                 bg=kit.with_alpha(badge_color, 235))

    line = footer or ""
    if line:
        draw.text((10, height - 22), kit.truncate(draw, line, small, width - 60),
                  font=small, fill=theme.color("color_text"))

    # 등급 보석 — 아래쪽 왼쪽. 숫자보다 개수가 빨리 읽힌다. footer 가 그
    # 자리를 이미 쓰면 그 옆으로 붙인다.
    gem_y = height - 16
    gem_x0 = 10 + (kit.text_size(draw, line, small)[0] + 10 if line else 0)
    for index in range(min(rarity, 6) if graded else 0):
        gem_x = gem_x0 + index * 11
        draw.ellipse((gem_x, gem_y, gem_x + 7, gem_y + 7),
                     fill=theme.color("color_disabled_border") if dimmed else border_color)

    upgrade = int(card.get("upgrade_tier") or 0)
    if upgrade:
        # 아래 오른쪽. 그림 위(왼쪽 위)에 쓰면 카드를 작게 그렸을 때 이름과
        # 겹치고, 오른쪽 위는 덱 화면의 장수 뱃지 자리다.
        text = f"+{upgrade}"
        draw.text((width - draw.textlength(text, font=small) - 10, height - 20),
                  text, font=small, fill=theme.color("color_accent"))

    return image


def _rarity_border(theme, rarity: int):
    """등급 테두리 색. 표에 없으면 기존 희귀도 색으로 물러선다."""
    table = theme.get("color_by_rarity_border", {})
    entry = table.get(str(rarity))
    if entry is None:
        return theme.rarity_color(rarity)
    return (int(entry[0]), int(entry[1]), int(entry[2]))


def _card_kind_color(theme, category: str, element=None):
    """카드 종류 색. 종류를 모르면 속성 색으로 물러선다."""
    table = theme.get("color_by_card_kind", {})
    for key, value in table.items():
        if key and key in category:
            return (int(value[0]), int(value[1]), int(value[2]))
    return theme.element_color(element)


# =====================================================================
# 전투
# =====================================================================
def _timed_effect_label(entry: dict) -> str:
    """`battle_timed_effects` 한 행을 짧은 표시로 (§2.5.3).

    무적·라운드 한정 스탯 변화는 전투 계산에는 이미 반영되지만, 화면에는
    지금까지 나온 적이 없었다 — 대미지가 왜 0인지, 공격력이 왜 갑자기
    달라졌는지 플레이어가 알 방법이 없었다."""
    if entry.get("effect_kind") == "invulnerable":
        return "무적"
    stat = entry.get("stat", "")
    delta = float(entry.get("delta", 0))
    sign = "+" if delta >= 0 else ""
    suffix = "%" if entry.get("is_percent") else ""
    return f"{stat}{sign}{delta:g}{suffix}"


def _status_chips(unit: dict) -> list[str]:
    """상태이상과 시간제 효과를 배지 하나씩으로 나눈다.

    한 줄 문자열로 이어 붙이면 칸이 좁을 때 통째로 잘려 무엇이 걸렸는지
    전혀 보이지 않는다. 배지로 나누면 들어가는 만큼은 읽힌다.
    """
    chips = [f"{entry['status_id']} {entry['stacks']}"
             for entry in unit.get("statuses", [])]
    chips += [_timed_effect_label(entry)
              for entry in unit.get("timed_effects", [])]
    return [chip for chip in chips if chip]


def render_ally_panel(units: list[dict], *, resource: int, round_no: int,
                      canvas: Canvas | None = None) -> Image.Image:
    """아군 패널 — 카드형 칸, 체력, 방어막, 상태이상 (§11).

    파티 공유 자원(§2.3)은 숫자 대신 구슬로 그린다. 몇 개 남았는지는 세는
    것보다 보는 편이 빠르다.

    아군은 최대 3명뿐이라(§2.1) 옆으로 늘어놓고 그림을 크게 그린다 — 세로로
    쌓아 작은 초상화만 보이면 손패의 카드보다도 정보가 적어진다.
    """
    canvas = canvas or Canvas(theme_module.load().size("panel_size"),
                              tint="color_tint_battle")
    theme = canvas.theme
    canvas.title(f"라운드 {round_no}")
    _resource_orbs(canvas, resource)

    small = canvas.font("small")
    head = 52
    count = max(1, min(len(units), 3))
    slot_width = (canvas.width - canvas.pad * 2 - canvas.gap * (count - 1)) // count
    slot_height = canvas.height - head - canvas.pad

    for index, unit in enumerate(units[:3]):
        left = canvas.pad + index * (slot_width + canvas.gap)
        box = (left, head, left + slot_width, head + slot_height)
        alive = unit.get("is_alive", True)
        canvas.tile(box, dimmed=not alive,
                    outline=None if alive else theme.color("color_disabled_border"))

        # 그림 — 칸 안쪽을 거의 꽉 채운다. 이름·체력은 그림 위에 얹고,
        # 위아래만 어둡게 깔아(fade) 글자 자리를 대신한다.
        art = canvas.assets.art(
            "character", str(unit.get("character_id", unit.get("name", ""))), label="",
            rarity=unit.get("tier", 1), size=(slot_width - 12, slot_height - 12))
        canvas.art_tile((left + 6, head + 6, left + slot_width - 6, head + slot_height - 6),
                        art, grayscale=not alive, fade_top=True, fade_bottom=True,
                        outline=theme.color("color_border"))

        text_left = left + 10
        text_top = head + 8
        canvas.label((text_left, text_top),
                     kit.truncate(canvas.draw, str(unit.get("name", "?")),
                                  canvas.font("body"), slot_width - 20),
                     color=theme.color("color_muted") if not alive else None)

        # 성급 — 등급과 다른 축이라 별로만 표시한다 (§4.4).
        star = int(unit.get("star") or unit.get("tier") or 0)
        if star:
            kit.stars(canvas.draw, (text_left, text_top + 24), star, max(star, 3), small,
                      color=theme.color("color_accent"),
                      dim=theme.color("color_disabled_border"))

        if not alive:
            canvas.label((text_left, head + slot_height - 30), "전투불능",
                         color=theme.color("color_hp_crit"))
            continue

        bar_width = slot_width - 20
        bar_top = head + slot_height - 58
        ratio = unit.get("hp_current", 0) / max(1, unit.get("hp_max", 1))
        high, low = canvas.hp_colors(ratio)
        kit.gauge(canvas.image, (text_left, bar_top, text_left + bar_width, bar_top + 14),
                  ratio, high=high, low=low, back=theme.color("color_gauge_back"))
        canvas.label((text_left, bar_top + 16),
                     f"{unit.get('hp_current', 0)}/{unit.get('hp_max', 1)}", role="small")

        cursor = text_left
        row = bar_top + 36
        if unit.get("block"):
            cursor += kit.pill(canvas.image, (cursor, row), f"방어 {unit['block']}", small,
                               fg=theme.color("color_block_text"),
                               bg=theme.color("color_block_back"),
                               border=theme.color("color_block")) + 5
        for text in _status_chips(unit)[:3]:
            if cursor > left + slot_width - 30:
                cursor = text_left
                row += 22
            cursor += canvas.pill((cursor, row), text, color=theme.color("color_text"),
                                  back=theme.color("color_gauge_back")) + 5
    return canvas.image


def _resource_orbs(canvas: Canvas, resource: int) -> None:
    """§2.3 파티 공유 자원. 오른쪽 위에 구슬로 그린다."""
    theme = canvas.theme
    shown = max(resource, 0)
    radius = 10
    step = radius * 2 + 6
    origin_x = canvas.width - canvas.pad - shown * step
    label = "자원"
    font = canvas.font("small")
    canvas.draw.text((origin_x - canvas.draw.textlength(label, font=font) - 8,
                      canvas.pad // 2 + 4), label, font=font, fill=canvas.muted)
    for index in range(shown):
        cx = origin_x + index * step + radius
        cy = canvas.pad // 2 + 12
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        kit.outer_glow(canvas.image, box, theme.color("color_accent"),
                       radius=radius, blur=7, opacity=110, spread=1)
        orb = kit.linear_gradient((radius * 2, radius * 2),
                                  kit.mix(theme.color("color_accent"), (255, 255, 255), 0.45),
                                  theme.color("color_accent_dark"))
        orb.putalpha(kit.rounded_mask((radius * 2, radius * 2), radius))
        canvas.image.alpha_composite(orb, (cx - radius, cy - radius))


def render_enemy_panel(units: list[dict], telegraphs: dict[int, dict],
                       canvas: Canvas | None = None) -> Image.Image:
    """적 패널 — **모든** 적의 행동 예고를 포함한다 (§11).

    이미 행동한 적은 `행동 완료`를 보여준다. 빈칸도 아니고, 다음 라운드
    계획을 미리 지어내지도 않는다 (§2.8.6).

    적 수는 스테이지마다 다르고 상한이 없으므로(§2.6) 칸을 격자로 배치한다.
    """
    canvas = canvas or Canvas(theme_module.load().size("panel_size"),
                              tint="color_tint_battle")
    theme = canvas.theme
    canvas.title("적")

    enemy = theme.color("color_enemy")
    enemy_dark = theme.color("color_enemy_dark")
    small = canvas.font("small")

    shown = units[:8] or [None]
    # 칸을 채우는 대신, 실제 마릿수만큼만 줄을 써서 그림 자리를 넓힌다 — 적이
    # 한둘일 때도 8마리를 다 채운 것처럼 그림이 작아지지 않게 한다.
    columns = min(4, len(shown))
    rows = -(-len(shown) // columns)
    available_height = canvas.height - 48 - canvas.pad
    cell_width = (canvas.width - canvas.pad * 2 - canvas.gap * (columns - 1)) // columns
    cell_height = (available_height - canvas.gap * (rows - 1)) // rows

    for index, unit in enumerate(units[:8]):
        column, row = index % columns, index // columns
        left = canvas.pad + column * (cell_width + canvas.gap)
        top = 48 + row * (cell_height + canvas.gap)
        alive = unit.get("is_alive", True)
        canvas.tile((left, top, left + cell_width, top + cell_height),
                    dimmed=not alive, outline=None if not alive else enemy_dark)

        # 그림 — 칸 안쪽을 거의 꽉 채운다. 이름·체력·상태는 그림 위에 얹고,
        # 위아래만 어둡게 깔아(fade) 글자 자리를 대신한다. 손패 카드와 같은
        # 언어로, 작은 초상화 하나로는 무엇을 상대하는지 한눈에 들어오지
        # 않는다.
        roomy = cell_height >= 160
        art = canvas.assets.art(
            "enemy", str(unit.get("enemy_id", unit.get("name", ""))), label="",
            rarity=unit.get("tier", 1), size=(cell_width - 12, cell_height - 12))
        canvas.art_tile((left + 6, top + 6, left + cell_width - 6, top + cell_height - 6),
                        art, grayscale=not alive, fade_top=True, fade_bottom=True,
                        outline=theme.color("color_disabled_border") if not alive else enemy_dark)

        # 대상 번호 — 카드를 낼 때 이 번호로 적을 고른다.
        canvas.draw.ellipse((left + 4, top + 4, left + 24, top + 24),
                            fill=theme.color("color_disabled_border") if not alive else enemy_dark,
                            outline=theme.color("color_disabled_border") if not alive else enemy,
                            width=2)
        number = str(index + 1)
        canvas.draw.text((left + 14 - canvas.draw.textlength(number, font=small) / 2,
                          top + 8), number, font=small, fill=canvas.text)

        text_left = left + 30
        canvas.label((text_left, top + 8),
                     kit.truncate(canvas.draw, str(unit.get("name", "?")), small,
                                  cell_width - 40),
                     role="small",
                     color=theme.color("color_muted") if not alive else None)

        # 아래쪽부터 쌓는다 — 그림이 칸을 꽉 채우므로 위쪽 여백 크기와
        # 무관하게 체력·상태·행동 예고 자리를 늘 같은 높이에 둘 수 있다.
        telegraph_top = top + cell_height - 26
        chip_row_top = telegraph_top - 24
        hp_text_top = (chip_row_top if roomy else telegraph_top) - 16
        bar_top = hp_text_top - 14

        if alive:
            ratio = unit.get("hp_current", 0) / max(1, unit.get("hp_max", 1))
            high, low = canvas.hp_colors(ratio)
            kit.gauge(canvas.image,
                      (left + 10, bar_top, left + cell_width - 10, bar_top + 10),
                      ratio, high=high, low=low, back=theme.color("color_gauge_back"))
            hp_text = f"{unit.get('hp_current', 0)}/{unit.get('hp_max', 1)}"
            if not roomy and unit.get("block"):
                # 칸이 낮아 배지 줄을 뺄 때도 방어막만큼은 체력 옆에 남긴다 —
                # 그 수치가 빠지면 다음 피해가 왜 그대로 들어갔는지 안 보인다.
                hp_text += f" · 방어 {unit['block']}"
            canvas.label((left + 10, hp_text_top), hp_text,
                         role="small", color=theme.color("color_text"))

            # 적 상태이상·시간제 효과 — 화상이나 방어력 감소가 화면에
            # 보이지 않으면 대미지가 왜 달라졌는지 알 수 없다 (§2.5.3).
            if roomy:
                chip_x, chip_y = left + 10, chip_row_top
                if unit.get("block"):
                    chip_x += kit.pill(canvas.image, (chip_x, chip_y),
                                       f"방어 {unit['block']}", small,
                                       fg=theme.color("color_block_text"),
                                       bg=theme.color("color_block_back")) + 4
                for text in _status_chips(unit)[:2]:
                    if chip_x > left + cell_width - 40:
                        break
                    chip_x += kit.pill(canvas.image, (chip_x, chip_y),
                                       kit.truncate(canvas.draw, text, small, 70), small,
                                       fg=theme.color("color_text"),
                                       bg=theme.color("color_gauge_back")) + 4

        telegraph = telegraphs.get(unit.get("battle_unit_id"), {})
        label = telegraph.get("label", "행동 완료")
        planned = telegraph.get("state") == "planned"
        kit.pill(canvas.image, (left + 8, telegraph_top),
                 kit.truncate(canvas.draw, f"→ {label}", small, cell_width - 24), small,
                 fg=canvas.text if planned else theme.color("color_muted"),
                 bg=enemy_dark if planned else theme.color("color_gauge_back"))

        if not alive:
            kit.pill(canvas.image, (left + cell_width - 52, top + 30), "격파", small,
                     fg=theme.color("color_hp_crit"),
                     bg=theme.color("color_disabled"))
    return canvas.image


def _render_log_strip(theme: theme_module.Theme, width: int, log: list[str]) -> Image.Image:
    """전투 최근 기록 — 계산은 이미 다 되고 있었지만(§2.11) 화면 어디에도
    보인 적이 없던 정보다. 몇 줄을 보여줄지는 `battle_log_lines` 로 정한다."""
    lines_shown = theme.int_("battle_log_lines")
    small = theme.font(role="small")
    line_height = theme.int_("font_size_small") + 6
    pad = 10
    height = pad * 2 + 22 + lines_shown * line_height

    image = Image.new("RGB", (width, height), theme.color("color_panel"))
    draw = ImageDraw.Draw(image)
    draw.line([(0, 0), (width, 0)], fill=theme.color("color_border"), width=2)
    draw.text((pad, pad), "전투 기록", font=small, fill=theme.color("color_muted"))

    shown = log[-lines_shown:] if log else ["아직 기록이 없습니다"]
    top = pad + 22
    for index, entry in enumerate(shown):
        draw.text((pad, top + index * line_height), kit.truncate(draw, entry, small, width - pad * 2),
                  font=small, fill=theme.color("color_text"))
    return image


def render_battle_screen(ally_units: list[dict], enemy_units: list[dict],
                         telegraphs: dict[int, dict], *, resource: int,
                         round_no: int, hand: list[dict] | None = None,
                         passives: list[dict] | None = None,
                         log: list[str] | None = None
                         ) -> list[Attachment]:
    """§1.3.7 — 중앙봇은 한 action 에 PNG 두 장까지만 받는다.

    한 장은 **전투 참가자**(적·아군, 그 아래 최근 로그), 다른 한 장은
    **이번 턴에 쓸 것**(손패·장착 패시브·자원)으로 나눈다 — "누가
    싸우는가"와 "내가 뭘 낼 수 있는가"는 서로 다른 질문이라, 한 장에
    아군과 손패를 섞어 두면 어느 쪽을 보려는지 매번 다시 찾아야 했다."""
    theme = theme_module.load()
    assets = AssetLibrary(theme)
    size = theme.size("panel_size")

    enemy_image = render_enemy_panel(
        enemy_units, telegraphs, canvas=Canvas(size, theme, assets))
    ally_image = render_ally_panel(
        ally_units, resource=resource, round_no=round_no,
        canvas=Canvas(size, theme, assets))
    log_image = _render_log_strip(theme, size[0], log or [])

    combatants = Image.new("RGB", (size[0], size[1] * 2 + log_image.height),
                           theme.color("color_background"))
    combatants.paste(enemy_image, (0, 0))
    combatants.paste(ally_image, (0, size[1]))
    combatants.paste(log_image, (0, size[1] * 2))

    hand_image = render_hand(
        hand or [], resource=resource, passives=passives,
        canvas=Canvas(size, theme, assets))

    return [
        to_attachment(combatants, "deckout_combatants.png"),
        to_attachment(hand_image, "deckout_hand.png"),
    ]


def render_hand(hand: list[dict], *, resource: int,
                canvas: Canvas | None = None,
                passives: list[dict] | None = None) -> Image.Image:
    """손패 · 장착 패시브 · 자원 — 이번 턴에 쓸 수 있는 것 전부 (§11).

    낼 수 없는 카드는 어둡게, 목록에서 빼지는 않는다.

    턴당 뽑는 장수는 설정으로 바뀔 수 있으므로(§2.2), 폭이 모자라면 카드를
    잘라내지 않고 **줄여서** 전부 보여준다. 한 장이라도 화면 밖으로 밀리면
    무엇을 뽑았는지 알 수 없게 되어 선택 자체가 성립하지 않는다.
    """
    canvas = canvas or Canvas(theme_module.load().size("panel_size"),
                              tint="color_tint_battle")
    canvas.title("손패", right=f"자원 {resource}")

    hand_top = 56
    if passives:
        hand_top = _passives_row(canvas, passives, top=56)

    if not hand:
        canvas.label((canvas.pad, hand_top), "낼 수 있는 카드가 없습니다",
                     color=canvas.muted)
        return canvas.image

    card_width, card_height = canvas.theme.size("card_size")
    count = len(hand)
    room = canvas.width - canvas.pad * 2 - canvas.gap * (count - 1)
    scale = min(1.0,
                (canvas.height - hand_top - 14) / card_height,
                (room / count) / card_width)
    size = (max(1, int(card_width * scale)), max(1, int(card_height * scale)))

    total = count * size[0] + canvas.gap * (count - 1)
    start = max(canvas.pad, (canvas.width - total) // 2)
    for index, card in enumerate(hand):
        affordable = int(card.get("cost", 0)) <= resource
        art = render_card(card, canvas, size=size, dimmed=not affordable)
        canvas.paste(art, (start + index * (size[0] + canvas.gap), hand_top))
    return canvas.image


def _passives_row(canvas: Canvas, passives: list[dict], *, top: int) -> int:
    """장착 패시브를 손패 위에 한 줄로. 돌려주는 값은 그 아래, 손패가 시작할
    y 좌표다 — 패시브가 없으면 손패가 그 자리를 그대로 쓴다."""
    theme = canvas.theme
    canvas.label((canvas.pad, top), "장착 패시브", role="small", color=canvas.muted)

    tile = 56
    art_top = top + 18
    x = canvas.pad
    for entry in passives[:8]:
        art = canvas.assets.art(
            "passive", str(entry.get("passive_card_id", "")),
            label=str(entry.get("name", "")), rarity=entry.get("rarity_tier"),
            size=(tile, tile))
        canvas.paste(art, (x, art_top))
        canvas.draw.rounded_rectangle(
            (x, art_top, x + tile - 1, art_top + tile - 1),
            radius=max(1, canvas.radius - 4), outline=canvas.accent, width=2)
        x += tile + canvas.gap
    return art_top + tile + 12


# =====================================================================
# 지도
# =====================================================================
def render_map(nodes: list[dict], edges: list[tuple[int, int]], *,
               current_node_index: int | None,
               available: set[int] | None = None,
               visited: set[int] | None = None,
               world_id: str | None = None) -> Attachment:
    """지도 화면 — 지도 전체를 한 장으로 (§11).

    칸은 네 가지로 그려진다: 지금 서 있는 칸, 갈 수 있는 칸, 지나온 칸,
    그리고 아직 닿지 않은 칸. 지나온 칸과 닿지 않은 칸을 같은 색으로 그리면
    지도가 어디까지 왔는지 말해 주지 못한다.

    진행 방향은 **왼쪽에서 오른쪽**이다. 층이 깊어질수록 세로로 늘리면
    디스코드에서 이미지가 축소되어 칸 글씨가 뭉개진다.
    """
    canvas = Canvas(theme_module.load().size("map_size"), tint="color_tint_map")
    available = available or set()
    visited = visited or set()
    theme = canvas.theme

    if world_id:
        # 월드 그림을 넣어 두었으면 바탕으로 깐다. 없으면 그냥 배경색이다.
        backdrop = canvas.assets.load("world", world_id,
                                      (canvas.width, canvas.height))
        if backdrop is not None:
            canvas.image.paste(backdrop.convert("RGB"), (0, 0))
            veil = Image.new("RGBA", (canvas.width, canvas.height),
                             (*theme.color("color_background"), 190))
            canvas.image.paste(veil, (0, 0), veil)
            canvas.draw = ImageDraw.Draw(canvas.image)

    by_depth: dict[int, list[dict]] = {}
    for node in nodes:
        by_depth.setdefault(node["depth"], []).append(node)
    max_depth = max(by_depth) if by_depth else 1
    widest = max((len(row) for row in by_depth.values()), default=1)

    head = 58
    foot = 26
    radius = 22
    boss_radius = 28
    span_x = canvas.width - canvas.pad * 2 - boss_radius * 2
    band = canvas.height - head - foot
    center_y = head + band / 2

    positions: dict[int, tuple[int, int]] = {}
    for depth, row in sorted(by_depth.items()):
        x = canvas.pad + boss_radius + int(
            span_x * (depth - 1) / max(1, max_depth - 1))
        step = band / max(2, widest + 1)
        ordered = sorted(row, key=lambda n: n["node_index"])
        top = center_y - step * (len(ordered) - 1) / 2
        for index, node in enumerate(ordered):
            positions[node["node_index"]] = (x, int(top + index * step))

    # ---- 길 ----
    walked = theme.color("color_visited")
    for source, target in edges:
        if source not in positions or target not in positions:
            continue
        live = source == current_node_index and target in available
        been = source in visited and target in visited
        if live:
            color, width = theme.color("color_selectable"), 3
        elif been:
            color, width = walked, 3
        else:
            color, width = theme.color("color_border"), 2
        canvas.draw.line(_curve(positions[source], positions[target]),
                         fill=color, width=width, joint="curve")

    # ---- 칸 ----
    small = canvas.font("small")
    for node in nodes:
        index = node["node_index"]
        if index not in positions:
            continue
        x, y = positions[index]
        node_type = str(node["node_type"])
        is_boss = "보스" in node_type
        r = boss_radius if is_boss else radius
        color = theme.node_color(node_type)

        state = ("current" if index == current_node_index
                 else "open" if index in available
                 else "past" if index in visited else "locked")

        if state in ("current", "open"):
            kit.outer_glow(canvas.image, (x - r, y - r, x + r, y + r), color,
                           radius=r, blur=13,
                           opacity=theme.int_("glow_opacity_available"), spread=3)

        if state == "locked":
            top, bottom = kit.shade(color, 0.34), theme.color("color_background")
            outline = theme.color("color_border")
        elif state == "past":
            top, bottom = kit.shade(color, 0.62), kit.shade(color, 0.34)
            outline = walked
        else:
            top, bottom = kit.mix(color, (255, 255, 255), 0.18), kit.shade(color, 0.62)
            outline = kit.mix(color, (255, 255, 255), 0.30)

        disc = kit.linear_gradient((r * 2, r * 2), top, bottom)
        disc.putalpha(kit.rounded_mask((r * 2, r * 2), r))
        canvas.image.alpha_composite(disc, (x - r, y - r))
        canvas.draw.ellipse((x - r, y - r, x + r, y + r), outline=outline, width=2)

        if state == "current":
            # 지금 서 있는 칸 — 칸 색은 그대로 두고 둘레에 이중 링을 두른다.
            # 색만 바꾸면 "보상 칸"처럼 보여 칸 종류를 잘못 읽게 된다.
            canvas.draw.ellipse((x - r - 3, y - r - 3, x + r + 3, y + r + 3),
                                outline=theme.color("color_text"), width=2)
            canvas.draw.ellipse((x - r - 7, y - r - 7, x + r + 7, y + r + 7),
                                outline=theme.color("color_selectable"), width=2)

        # 칸 이름은 한 글자로 안에, 전체 이름은 아래에 적는다.
        # "보상"과 "보스"는 첫 글자가 겹쳐 구분이 되지 않으므로 보스만 따로 쓴다.
        head_letter = "王" if is_boss else node_type[:1]
        offset = canvas.draw.textlength(head_letter, font=canvas.font("body")) / 2
        canvas.draw.text((x - offset, y - 11), head_letter, font=canvas.font("body"),
                         fill=theme.color("color_background")
                         if state in ("current", "open") else theme.color("color_muted"))
        label = kit.truncate(canvas.draw, node_type, small, 84)
        canvas.draw.text((x - canvas.draw.textlength(label, font=small) / 2, y + r + 5),
                         label, font=small,
                         fill=theme.color("color_text")
                         if state in ("current", "open") else theme.color("color_muted"))

        if state == "past":
            canvas.draw.ellipse((x + r - 11, y - r - 1, x + r + 1, y - r + 11),
                                fill=theme.color("color_hp_full"),
                                outline=theme.color("color_background"), width=2)

    depth_now = next((n["depth"] for n in nodes
                      if n["node_index"] == current_node_index), 0)
    canvas.title("지도", right=f"{depth_now} / {max_depth} 층")
    return canvas.finish("deckout_map.png")


def _curve(start: tuple[int, int], end: tuple[int, int], steps: int = 24
           ) -> list[tuple[int, int]]:
    """가로 진행에 맞춘 S자 곡선. 직선 다발보다 길이 눈에 잘 따라온다."""
    (x0, y0), (x1, y1) = start, end
    mid = (x0 + x1) / 2
    points = []
    for step in range(steps + 1):
        t = step / steps
        inv = 1 - t
        x = inv**3 * x0 + 3 * inv**2 * t * mid + 3 * inv * t**2 * mid + t**3 * x1
        y = inv**3 * y0 + 3 * inv**2 * t * y0 + 3 * inv * t**2 * y1 + t**3 * y1
        points.append((int(x), int(y)))
    return points


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
    """뽑기 결과 — 뽑은 것 전부를 한 장에 (§5.9).

    카드 틀을 그대로 써서 등급 테두리와 종류가 그대로 보이게 한다. 결과창만
    다른 모양으로 그리면 방금 뽑은 것이 손패에서 어떤 카드였는지 이어지지
    않는다.

    캐릭터는 **성급**(별)로, 카드는 **등급**(보석)으로 표시가 갈린다 — 성급은
    같은 캐릭터를 몇 장 모았는가이고, 등급은 가챠에서 얼마나 안 나오는가라
    서로 다른 축이다.
    """
    canvas = Canvas(theme_module.load().size("gacha_result_size"),
                    tint="color_tint_gacha")
    canvas.title(f"뽑기 결과 {len(results)}회")

    if not results:
        canvas.label((canvas.pad, 64), "뽑은 것이 없습니다", color=canvas.muted)
        return canvas.finish("deckout_gacha.png")

    card_size = canvas.theme.size("card_size")
    note_room = 22
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (int(card_size[0] * 0.8) + canvas.gap))
    rows = max(1, -(-len(results) // columns))

    # 결과 수가 많아도 잘라내지 않고 줄여서 전부 보여준다 (10연 기준).
    scale = min(0.9,
                (canvas.width - canvas.pad * 2 - canvas.gap * (columns - 1))
                / (columns * card_size[0]),
                (canvas.height - 62 - canvas.pad - (rows - 1) * canvas.gap
                 - rows * note_room) / (rows * card_size[1]))
    size = (max(1, int(card_size[0] * scale)), max(1, int(card_size[1] * scale)))

    total_width = columns * size[0] + canvas.gap * (columns - 1)
    origin_x = max(canvas.pad, (canvas.width - total_width) // 2)

    for index, result in enumerate(results):
        column, row = index % columns, index // columns
        left = origin_x + column * (size[0] + canvas.gap)
        top = 62 + row * (size[1] + canvas.gap + note_room)

        star = result.get("star_rank")
        card = {
            "card_id": result.get("entity_id", ""),
            "name": result.get("name", result.get("entity_id", "")),
            "category": result.get("category", ""),
            "rarity_tier": result.get("rarity_tier"),
            "element": result.get("element"),
        }
        canvas.paste(render_card(card, canvas, size=size), (left, top))

        if star:
            # 캐릭터는 등급 보석 대신 성급 별을 얹는다.
            kit.stars(canvas.draw, (left + 10, top + size[1] - 20),
                      int(star), max(int(star), 3), canvas.font("small"),
                      color=canvas.theme.color("color_accent"),
                      dim=canvas.theme.color("color_disabled_border"))

        note = ""
        color = canvas.accent
        if result.get("is_duplicate"):
            note = f"중복 · 조각 {result.get('fragments', 0)}"
            color = canvas.muted
        elif result.get("forced"):
            note = "확정"
        elif result.get("is_new"):
            note = "신규"
        if note:
            kit.pill(canvas.image, (left + 2, top + size[1] + 3),
                     kit.truncate(canvas.draw, note, canvas.font("small"), size[0] - 8),
                     canvas.font("small"), fg=color,
                     bg=canvas.theme.color("color_accent_back") if color == canvas.accent
                     else canvas.theme.color("color_gauge_back"))
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
# 허브 — 요약, 캐릭터, 장비, 연구, 업적
# =====================================================================
def render_hub(account: dict, *, coin: int | None, daily: dict,
               note: str | None = None) -> Attachment:
    """허브 요약 — 재화, 슬롯, 출석 (§4.6).

    허브 화면은 글자만 있었다. 다른 화면은 전부 그림 한 장을 붙이는데
    여기만 빠져 있었던 이유는 그릴 만한 '내용물'(카드·캐릭터·지도)이 없기
    때문이었다 — 그래서 숫자 자체를 패널로 그린다.
    """
    canvas = Canvas(theme_module.load().size("panel_size"))
    canvas.title("덱아웃")

    top = 56
    if note:
        canvas.label((canvas.pad, top), note[:60], role="small", color=canvas.muted)
        top += 22

    coin_text = f"코인 {coin}" if coin is not None else "코인 —"
    canvas.label((canvas.pad, top), coin_text, color=canvas.accent)
    canvas.label((canvas.pad, top + 26),
                 f"카르타 {account.get('carta', 0)} · "
                 f"와일드카드 {account.get('wildcards', 0)}")
    canvas.label((canvas.pad, top + 50),
                 f"파티 슬롯 {account.get('party_slots', 0)} · "
                 f"패시브 슬롯 {account.get('passive_slots', 0)}", role="small",
                 color=canvas.muted)

    daily_top = top + 86
    if daily.get("claimable"):
        reward = daily.get("reward", {})
        canvas.label((canvas.pad, daily_top),
                     f"출석 {daily.get('streak', 0)}일째 — 받을 것: "
                     f"코인 {reward.get('coin', 0)} · "
                     f"카르타 {reward.get('carta', 0)}", color=canvas.accent)
    else:
        canvas.label((canvas.pad, daily_top),
                     f"출석 {daily.get('streak', 0)}일째 — 오늘 것은 받았습니다.",
                     role="small", color=canvas.muted)
    return canvas.finish("deckout_hub.png")


def render_characters(rows: list[dict]) -> Attachment:
    """보유 캐릭터 명단 — 성급과 다음 성급 비용 (§4.4)."""
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title("캐릭터", right=f"보유 {len(rows)}")

    portrait = (64, 64)
    item_width, row_height = 420, 80
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (item_width + canvas.gap))
    capacity_rows = max(1, (canvas.height - 56 - canvas.pad + canvas.gap)
                        // (row_height + canvas.gap))
    capacity = columns * capacity_rows

    for index, entry in enumerate(rows[:capacity]):
        column, slot = index % columns, index // columns
        left = canvas.pad + column * (item_width + canvas.gap)
        top = 56 + slot * (row_height + canvas.gap)
        art = canvas.assets.art("character", str(entry.get("character_id", "")),
                                label=str(entry.get("name", "")),
                                rarity=entry.get("star_rank"), size=portrait)
        canvas.paste(art, (left, top))

        text_left = left + portrait[0] + 10
        star = "★" * int(entry.get("star_rank", 1))
        canvas.label((text_left, top), f"{entry.get('name', '')} {star}"[:20])
        canvas.label((text_left, top + 22),
                     f"{entry.get('element', '')} · {entry.get('job_role', '')}",
                     role="small", color=canvas.muted)
        status = entry.get("status")
        if status:
            canvas.label((text_left, top + 44), str(status)[:28], role="small",
                         color=canvas.accent if entry.get("status_ready")
                         else canvas.muted)

    hidden = len(rows) - capacity
    if hidden > 0:
        canvas.label((canvas.pad, canvas.height - 24), f"그 외 {hidden}명",
                     role="small", color=canvas.muted)
    return canvas.finish("deckout_characters.png")


def render_equipment(rows: list[dict], *, stones: list[dict] | None = None) -> Attachment:
    """보유 장비 명단 — 티어, 장착 대상, 다음 강화 비용 (§8.4)."""
    canvas = Canvas(theme_module.load().size("shop_size"))
    canvas.title("장비", right=f"보유 {len(rows)}")

    top0 = 56
    if stones:
        canvas.label((canvas.pad, top0), "강화석  " + " · ".join(
            f"T{row['tier']}×{row['amount']}" for row in stones), role="small",
            color=canvas.accent)
        top0 += 26

    icon = (56, 56)
    item_width, row_height = 420, 72
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (item_width + canvas.gap))
    capacity_rows = max(1, (canvas.height - top0 - canvas.pad + canvas.gap)
                        // (row_height + canvas.gap))
    capacity = columns * capacity_rows

    for index, entry in enumerate(rows[:capacity]):
        column, slot = index % columns, index // columns
        left = canvas.pad + column * (item_width + canvas.gap)
        top = top0 + slot * (row_height + canvas.gap)
        art = canvas.assets.art("equipment", str(entry.get("equipment_def_id", "")),
                                label=str(entry.get("name", "")), size=icon)
        canvas.paste(art, (left, top))

        text_left = left + icon[0] + 10
        canvas.label((text_left, top),
                     f"{entry.get('name', '')} T{entry.get('tier', 1)}"[:22])
        equipped = entry.get("equipped_character_id")
        sub = str(entry.get("slot", "")) + (f" · 장착: {equipped}" if equipped else "")
        canvas.label((text_left, top + 22), sub[:32], role="small", color=canvas.muted)
        need = entry.get("next_enhance")
        if need:
            canvas.label((text_left, top + 44), str(need)[:32], role="small",
                         color=canvas.accent)

    hidden = len(rows) - capacity
    if hidden > 0:
        canvas.label((canvas.pad, canvas.height - 24), f"그 외 {hidden}개",
                     role="small", color=canvas.muted)
    return canvas.finish("deckout_equipment.png")


def render_hub_shop(equipment: list[dict], stones: list[dict], *,
                    currency: int | None) -> Attachment:
    """허브 상점 진열 — 장비와 강화석 (§7.2)."""
    canvas = Canvas(theme_module.load().size("shop_size"))
    canvas.title("허브 상점", right=f"코인 {currency}" if currency is not None else "")

    icon = (56, 56)
    item_width, row_height = 420, 72
    columns = max(1, (canvas.width - canvas.pad * 2 + canvas.gap)
                  // (item_width + canvas.gap))
    for index, entry in enumerate(equipment):
        column, slot = index % columns, index // columns
        left = canvas.pad + column * (item_width + canvas.gap)
        top = 56 + slot * (row_height + canvas.gap)
        if top + row_height > canvas.height - 60:
            break
        art = canvas.assets.art("equipment", str(entry.get("equipment_def_id", "")),
                                label=str(entry.get("name", "")), size=icon)
        canvas.paste(art, (left, top))
        text_left = left + icon[0] + 10
        canvas.label((text_left, top), str(entry.get("name", ""))[:22])
        canvas.label((text_left, top + 22),
                     f"{entry.get('slot', '')} · 코인 {entry.get('price_coin', 0)}",
                     role="small", color=canvas.accent)

    stones_top = canvas.height - 46
    canvas.label((canvas.pad, stones_top), "강화석", role="small", color=canvas.muted)
    canvas.label((canvas.pad, stones_top + 20), " · ".join(
        f"T{entry.get('tier')} {entry.get('price_coin', 0)}" for entry in stones)[:80],
        role="small", color=canvas.accent)
    return canvas.finish("deckout_hub_shop.png")


def render_research(listing: list[dict]) -> Attachment:
    """연구 목록 — 잠김·해금 가능·완료와 다음 비용 (§20.5)."""
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title("연구")

    row_height = 44
    capacity = max(1, (canvas.height - 56 - canvas.pad + canvas.gap) // row_height)
    for index, entry in enumerate(listing[:capacity]):
        top = 56 + index * row_height
        completed = bool(entry.get("completed"))
        available = bool(entry.get("available"))
        locked = not completed and not available
        name_color = (canvas.theme.color("color_hp_full") if completed
                     else canvas.muted if locked else canvas.text)
        canvas.label((canvas.pad, top), str(entry.get("name", ""))[:32],
                     color=name_color)

        if completed:
            mark = "완료"
            width = canvas.draw.textlength(mark, font=canvas.font("small"))
            canvas.label((canvas.width - canvas.pad - width, top), mark,
                         role="small", color=canvas.theme.color("color_hp_full"))
        elif available:
            canvas.label((canvas.pad, top + 20),
                         f"코인 {entry.get('coin_cost', 0)} · "
                         f"와일드카드 {entry.get('wildcard_cost', 0)}",
                         role="small", color=canvas.accent)
        else:
            progress = entry.get("achievement_progress")
            if progress:
                canvas.bar(canvas.pad, top + 24, 200, 8,
                          int(progress.get("current", 0)),
                          int(progress.get("target", 1) or 1))
    return canvas.finish("deckout_research.png")


def render_achievements(listing: list[dict]) -> Attachment:
    """업적 목록 — 진행도 막대 (§20.5)."""
    canvas = Canvas(theme_module.load().size("prep_size"))
    canvas.title("업적")

    row_height = 40
    capacity = max(1, (canvas.height - 56 - canvas.pad + canvas.gap) // row_height)
    for index, entry in enumerate(listing[:capacity]):
        top = 56 + index * row_height
        completed = bool(entry.get("completed"))
        canvas.label((canvas.pad, top), str(entry.get("name", ""))[:36],
                     color=canvas.theme.color("color_hp_full") if completed
                     else canvas.text)
        canvas.bar(canvas.pad, top + 20, 240, 8,
                  int(entry.get("current_value", 0)),
                  int(entry.get("target_value", 1) or 1))

    hidden = len(listing) - capacity
    if hidden > 0:
        canvas.label((canvas.pad, canvas.height - 24), f"그 외 {hidden}개",
                     role="small", color=canvas.muted)
    return canvas.finish("deckout_achievements.png")


def render_run_deck(rows: list[dict]) -> Attachment:
    """런 중 덱 — 캐릭터별 뽑을 더미·버린 더미·손패·저주 (§16.2.3)."""
    canvas = Canvas(theme_module.load().size("panel_size"))
    canvas.title("덱 — 진행 중인 런")

    row_height = 48
    for index, entry in enumerate(rows[:6]):
        top = 56 + index * row_height
        canvas.label((canvas.pad, top), str(entry.get("name", ""))[:20])
        line = (f"뽑을 더미 {entry.get('draw', 0)} · "
               f"버린 더미 {entry.get('discard', 0)} · "
               f"손패 {entry.get('hand', 0)}")
        cursed = int(entry.get("cursed", 0))
        if cursed:
            line += f" · 저주 {cursed}"
        canvas.label((canvas.pad, top + 22), line, role="small", color=canvas.muted)
    return canvas.finish("deckout_run_deck.png")


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
