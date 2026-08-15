"""화면을 구성하는 표현 도구 — 그라데이션, 발광, 그림자, 게이지, 배지.

`panels.py` 가 배치에 집중할 수 있도록 "어떻게 보이게 만드는가"를 여기로
모았다. 이 모듈은 색과 크기를 **직접 정하지 않는다.** 전부 인자로 받는다 —
수치는 `config/10_화면.toml` 이 정한다는 원칙(§11)을 그대로 지킨다.

디스코드에 올라가는 이미지가 이 게임의 유일한 화면이라, 평면 사각형과 단색
막대만으로는 정보 위계가 서지 않는다. 그림자로 층을 나누고, 발광으로 지금
주목할 곳을 가리키고, 그라데이션으로 표면에 깊이를 준다.
"""

from __future__ import annotations

import colorsys

from PIL import Image, ImageDraw, ImageFilter

Color = tuple[int, int, int]


# ---------------------------------------------------------------------------
# 색 계산
# ---------------------------------------------------------------------------


def mix(a: Color, b: Color, t: float) -> Color:
    """두 색을 t(0~1) 비율로 섞는다."""
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def shade(color: Color, factor: float) -> Color:
    """factor<1 이면 어둡게, >1 이면 밝게."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)  # type: ignore[return-value]


def with_alpha(color: Color, alpha: int) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], alpha)


# ---------------------------------------------------------------------------
# 글자 — 글꼴에 없는 글자 걸러내기
# ---------------------------------------------------------------------------

#: 번들 글꼴에 글리프가 없어 네모(두부)로 깨지는 구간.
#: 실측 결과 화살표(→)와 별(★☆)은 정상이라 남기고, 이모지·체크·도형 기호만
#: 걸러낸다. 디스코드 본문 텍스트에서는 이모지가 정상 표시되므로 제거는
#: 이미지 렌더링 경로에서만 한다.
_STRIP_RANGES = (
    (0x1F000, 0x1FAFF),   # 이모지 (보조 평면)
    (0xFE00, 0xFE0F),     # variation selector
    (0x2300, 0x23FF),     # 기술 기호
    (0x2600, 0x27BF),     # 기타 기호 / 딩벳
    (0x2B00, 0x2BFF),
)
_KEEP = {"★", "☆"}


def sanitize(text: str) -> str:
    """이미지에 그릴 문자열에서 렌더링 불가 문자를 제거한다."""
    out = []
    for ch in str(text):
        if ch in _KEEP:
            out.append(ch)
            continue
        if any(lo <= ord(ch) <= hi for lo, hi in _STRIP_RANGES):
            continue
        out.append(ch)
    return "".join(out).strip()


def text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return (box[2] - box[0], box[3] - box[1])


def truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    text = sanitize(text)
    if text_size(draw, text, font)[0] <= max_width:
        return text
    out = text
    while out and text_size(draw, out + "…", font)[0] > max_width:
        out = out[:-1]
    return out + "…"


def wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int,
         max_lines: int = 3) -> list[str]:
    """한글은 단어 경계가 드물어 글자 단위로 접는다."""
    text = sanitize(text)
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + ch
        if text_size(draw, trial, font)[0] > width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# 배경 / 그라데이션
# ---------------------------------------------------------------------------


def linear_gradient(size: tuple[int, int], top: Color, bottom: Color) -> Image.Image:
    """세로 선형 그라데이션. 1px 폭으로 만들고 늘려 비용을 줄인다."""
    width, height = size
    strip = Image.new("RGB", (1, max(1, height)))
    pixels = strip.load()
    for y in range(max(1, height)):
        pixels[0, y] = mix(top, bottom, y / max(1, height - 1))
    return strip.resize(size, Image.BILINEAR).convert("RGBA")


def radial_glow(size: tuple[int, int], color: Color, strength: float) -> Image.Image:
    """중앙이 밝은 방사형 광원. 배경에 깊이를 준다."""
    small = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(small).ellipse((8, 8, 56, 56), fill=int(255 * strength))
    mask = small.filter(ImageFilter.GaussianBlur(12)).resize(size, Image.BILINEAR)
    layer = Image.new("RGBA", size, with_alpha(color, 0))
    layer.putalpha(mask)
    return layer


def vignette(size: tuple[int, int], strength: float) -> Image.Image:
    """가장자리를 눌러 시선을 가운데로 모은다."""
    small = Image.new("L", (64, 64), 255)
    ImageDraw.Draw(small).ellipse((-14, -14, 78, 78), fill=0)
    mask = (small.filter(ImageFilter.GaussianBlur(16))
            .resize(size, Image.BILINEAR)
            .point(lambda v: int(v * strength)))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.putalpha(mask)
    return layer


def background(size: tuple[int, int], base: Color, tint: Color,
               *, glow: float = 0.42, veil: float = 0.55) -> Image.Image:
    """화면 공통 배경 — 그라데이션 + 상단 광원 + 비네트."""
    width, height = size
    image = linear_gradient(size, mix(base, tint, 0.35), shade(base, 0.6))
    image.alpha_composite(radial_glow((width, max(1, int(height * 0.7))), tint, glow),
                          (0, -int(height * 0.18)))
    image.alpha_composite(vignette(size, veil))
    return image


# ---------------------------------------------------------------------------
# 패널 / 그림자 / 발광
# ---------------------------------------------------------------------------


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def drop_shadow(target: Image.Image, box: tuple[int, int, int, int], *,
                radius: int, blur: int = 10, offset: tuple[int, int] = (0, 5),
                opacity: int = 130) -> None:
    """패널 아래 부드러운 그림자. 배경과 내용을 층으로 분리한다."""
    x0, y0, x1, y1 = box
    pad = blur * 3
    size = (x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad, pad, size[0] - pad - 1, size[1] - pad - 1),
        radius=radius, fill=(0, 0, 0, opacity))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    target.alpha_composite(layer, (x0 - pad + offset[0], y0 - pad + offset[1]))


def outer_glow(target: Image.Image, box: tuple[int, int, int, int], color: Color, *,
               radius: int, blur: int = 9, opacity: int = 150, spread: int = 3) -> None:
    """강조 요소 주변 발광 — 지금 차례인 유닛, 갈 수 있는 칸에 쓴다."""
    x0, y0, x1, y1 = box
    pad = blur * 3
    size = (x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad - spread, pad - spread, size[0] - pad - 1 + spread, size[1] - pad - 1 + spread),
        radius=radius + spread, fill=with_alpha(color, opacity))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    target.alpha_composite(layer, (x0 - pad, y0 - pad))


def panel(target: Image.Image, box: tuple[int, int, int, int], *,
          top: Color, bottom: Color, radius: int,
          border: Color | None = None, border_width: int = 2,
          shadow: bool = True, highlight: bool = True) -> None:
    """그라데이션 + 상단 하이라이트 + 그림자를 갖춘 칸."""
    x0, y0, x1, y1 = box
    width, height = max(1, x1 - x0), max(1, y1 - y0)

    if shadow:
        drop_shadow(target, box, radius=radius)

    fill = linear_gradient((width, height), top, bottom)
    fill.putalpha(rounded_mask((width, height), radius))
    target.alpha_composite(fill, (x0, y0))

    draw = ImageDraw.Draw(target)
    if highlight:
        # 위쪽 1px 밝은 선 — 표면이 서 있는 느낌을 준다.
        draw.rounded_rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), radius=max(1, radius - 1),
                               outline=with_alpha(shade(top, 1.35), 90), width=1)
    if border:
        draw.rounded_rectangle(box, radius=radius, outline=border, width=border_width)


def gauge(target: Image.Image, box: tuple[int, int, int, int], ratio: float, *,
          high: Color, low: Color, back: Color) -> None:
    """그라데이션 게이지. 단색 막대보다 잔량이 눈에 빨리 들어온다."""
    x0, y0, x1, y1 = box
    width, height = max(1, x1 - x0), max(1, y1 - y0)
    radius = max(1, height // 2)

    draw = ImageDraw.Draw(target)
    draw.rounded_rectangle(box, radius=radius, fill=back)

    ratio = max(0.0, min(1.0, ratio))
    filled = int(width * ratio)
    if filled > 2:
        filled = max(filled, radius * 2)
        bar = linear_gradient((filled, height), high, low)
        bar.putalpha(rounded_mask((filled, height), radius))
        target.alpha_composite(bar, (x0, y0))
        gloss_height = max(1, height // 2)
        gloss = Image.new("RGBA", (filled, gloss_height), (255, 255, 255, 34))
        gloss.putalpha(Image.eval(rounded_mask((filled, gloss_height), radius),
                                  lambda v: int(v * 0.30)))
        target.alpha_composite(gloss, (x0, y0 + 1))

    draw.rounded_rectangle(box, radius=radius,
                           outline=with_alpha(shade(low, 0.7), 200), width=1)


def pill(target: Image.Image, xy: tuple[int, int], text: str, font, *,
         fg: Color, bg: Color, border: Color | None = None,
         pad_x: int = 8, pad_y: int = 3) -> int:
    """작은 라벨 배지. 반환값은 그려진 폭이라 이어 붙이기 쉽다."""
    draw = ImageDraw.Draw(target)
    text = sanitize(text)
    tw, th = text_size(draw, text, font)
    width, height = tw + pad_x * 2, th + pad_y * 2 + 2
    box = (xy[0], xy[1], xy[0] + width, xy[1] + height)
    draw.rounded_rectangle(box, radius=height // 2, fill=bg,
                           outline=border, width=1 if border else 0)
    draw.text((xy[0] + pad_x, xy[1] + pad_y), text, font=font, fill=fg)
    return width


def stars(draw: ImageDraw.ImageDraw, xy: tuple[int, int], count: int, cap: int,
          font, *, color: Color, dim: Color) -> None:
    """★ 채움 / ☆ 빈칸. 성급은 등급과 다른 축이라 별로만 표시한다."""
    x, y = xy
    for index in range(max(count, cap)):
        glyph = "★" if index < count else "☆"
        draw.text((x, y), glyph, font=font, fill=color if index < count else dim)
        x += text_size(draw, glyph, font)[0] + 1


# ---------------------------------------------------------------------------
# 그림이 없을 때
# ---------------------------------------------------------------------------


def placeholder(size: tuple[int, int], seed: str, label: str, tint: Color, font=None,
                *, label_color: Color = (255, 255, 255)) -> Image.Image:
    """그림이 아직 없을 때 대신 그리는 바탕.

    §11 은 카드마다 고유 그림을 요구하지만 그건 아트 제작 일정에 달린 문제다.
    파일이 없어도 화면이 초라해지지 않도록 그라데이션과 사선을 깔고 이름을
    옅게 얹는다. 색은 희귀도/종류에서 받아 화면 전체와 따로 놀지 않게 한다.
    """
    width, height = size
    jitter = (abs(hash(seed)) % 24) / 100.0
    top = mix(shade(tint, 0.85 + jitter), (255, 255, 255), 0.10)
    bottom = shade(tint, 0.34)

    image = linear_gradient(size, top, bottom)
    draw = ImageDraw.Draw(image)
    for offset in range(-height, width, 16):
        draw.line((offset, height, offset + height, 0),
                  fill=with_alpha(shade(top, 1.20), 22), width=6)

    if label and font is not None:
        text = truncate(draw, label, font, width - 10)
        tw, th = text_size(draw, text, font)
        draw.text(((width - tw) // 2, (height - th) // 2), text, font=font,
                  fill=with_alpha(label_color, 118))
    return image


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """비율을 지키며 꽉 채우고 넘치는 부분을 잘라낸다."""
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    resized = image.resize((max(1, int(image.width * scale)),
                            max(1, int(image.height * scale))), Image.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def framed_art(target: Image.Image, box: tuple[int, int, int, int],
               art: Image.Image | None, *, radius: int,
               grayscale: bool = False, fade_bottom: bool = False,
               fade_top: bool = False) -> None:
    """그림을 둥근 틀에 넣어 합성한다.

    `fade_top`/`fade_bottom` 은 그림 위에 바로 글자를 얹을 때 쓴다 — 카드
    안을 그림으로 꽉 채우면 이름표나 배지를 놓을 단색 자리가 따로 없어서,
    글자가 앉는 쪽 가장자리만 어둡게 깔아 대신한다."""
    x0, y0, x1, y1 = box
    size = (max(1, x1 - x0), max(1, y1 - y0))
    if art is None:
        return
    art = cover(art.convert("RGBA"), size)
    if grayscale:
        art = art.convert("LA").convert("RGBA")

    mask = rounded_mask(size, radius)
    art.putalpha(mask)

    if fade_bottom or fade_top:
        fade = Image.new("RGBA", size, (0, 0, 0, 0))
        fade_draw = ImageDraw.Draw(fade)
        span = max(1, size[1] // 2)
        for index in range(span):
            # 가장자리(index=0)에도 최소한의 어둠을 깐다 — 순수 제곱 곡선은
            # 가장자리에서 거의 0이라, 글자를 그 자리에 바로 얹으면(예: 카드
            # 맨 위 이름) 그림이 밝을 때 안 읽힌다.
            alpha = int(55 + 135 * (index / span) ** 1.6)
            if fade_bottom:
                fade_draw.line((0, size[1] - index - 1, size[0], size[1] - index - 1),
                               fill=(0, 0, 0, alpha))
            if fade_top:
                fade_draw.line((0, index, size[0], index), fill=(0, 0, 0, alpha))
        fade.putalpha(Image.composite(fade.getchannel("A"),
                                      Image.new("L", size, 0), mask))
        art.alpha_composite(fade)

    target.alpha_composite(art, (x0, y0))


def hue_shifted(color: Color, degrees: float) -> Color:
    """색상환에서 돌린 색. 같은 계열 안에서 변주를 줄 때 쓴다."""
    r, g, b = (c / 255 for c in color)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb((h + degrees / 360.0) % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))
