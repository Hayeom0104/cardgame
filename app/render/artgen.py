"""내용에서 그림을 만들어 낸다 — 진짜 그림이 올 때까지 자리를 지키는 용도.

§11의 대체 그림(이름 + 희귀도 색 한 판)은 "그림이 없어도 게임이 멈추지
않는다"는 것을 보장하지만, 화면 하나에 그 판이 열두 개 늘어서면 무엇이
무엇인지 알아볼 수 없다. 여기서 만드는 것은 그보다 한 단계 위다:

* **id 마다 다르게 생겼다.** id 를 해시해서 색과 무늬를 정하므로, 같은 id 는
  언제 만들어도 같은 그림이고 다른 id 는 확실히 다르게 보인다. 플레이어가
  그림의 생김새로 카드를 구별할 수 있다.
* **뜻이 담긴 색.** 원소가 있으면 원소 색을, 없으면 희귀도 색을 바탕에 쓴다.
  §10 화면 설정의 팔레트를 그대로 따르므로 화면과 따로 놀지 않는다.
* **덮어쓰지 않는다.** 진짜 그림이 이미 있으면 건드리지 않는다.

이것이 최종 아트를 대신하지는 않는다. 한 장씩 그려서 덮어 나가면 되고,
덮은 파일은 다음 화면부터 바로 쓰인다.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.render.assets import AssetLibrary
from app.render.theme import Theme

#: 그림을 이 배율로 크게 그린 뒤 줄인다. 계단 현상을 없애는 가장 싼 방법이다.
SUPERSAMPLE = 3

#: 이름 띠와 글자의 최종 크기(픽셀) 상한. 카드 한 장에는 넉넉하고, 월드 배경
#: 처럼 큰 그림에서는 이름이 화면을 덮지 않을 만큼이다.
MAX_LABEL_BAND = 34
MAX_LABEL_FONT = 14


def _seed(entity_id: str) -> int:
    return int.from_bytes(hashlib.sha256(entity_id.encode("utf-8")).digest()[:8],
                          "big")


def _tint(base: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """밝기만 옮긴다. 색상은 그대로 두어야 팔레트에서 벗어나지 않는다."""
    hue, lightness, saturation = colorsys.rgb_to_hls(*[value / 255 for value in base])
    lightness = min(1.0, max(0.0, lightness + amount))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (int(red * 255), int(green * 255), int(blue * 255))


def _gradient(size: tuple[int, int], top: tuple[int, int, int],
              bottom: tuple[int, int, int]) -> Image.Image:
    """위아래로 흐르는 바탕. 한 판 단색보다 훨씬 덜 심심하다."""
    width, height = size
    image = Image.new("RGB", (1, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.point((0, y), fill=tuple(
            int(top[index] + (bottom[index] - top[index]) * ratio)
            for index in range(3)))
    return image.resize((width, height), Image.BILINEAR)


def _emblem(draw: ImageDraw.ImageDraw, size: tuple[int, int], seed: int,
            color: tuple[int, int, int]) -> None:
    """id 에서 뽑아낸 무늬.

    꼭짓점 수와 회전각과 크기를 해시에서 가져오므로, 같은 id 는 언제나 같은
    도형이고 다른 id 는 한눈에 다르다.
    """
    width, height = size
    center = (width / 2, height * 0.44)
    radius = min(width, height) * (0.20 + (seed >> 8 & 0x3F) / 0x3F * 0.10)
    points = 3 + (seed & 0x7)                     # 3~10 각형
    rotation = (seed >> 16 & 0xFF) / 0xFF * math.tau
    inner = 0.42 + (seed >> 24 & 0x1F) / 0x1F * 0.35   # 별 모양의 깊이

    vertices = []
    for index in range(points * 2):
        angle = rotation + index * math.pi / points
        length = radius if index % 2 == 0 else radius * inner
        vertices.append((center[0] + math.cos(angle) * length,
                         center[1] + math.sin(angle) * length))

    draw.polygon(vertices, fill=(*color, 70), outline=(*color, 190))
    draw.ellipse([center[0] - radius * 0.16, center[1] - radius * 0.16,
                  center[0] + radius * 0.16, center[1] + radius * 0.16],
                 fill=(*color, 200))


def generate(theme: Theme, *, size: tuple[int, int], entity_id: str,
             label: str, rarity: int | None = None,
             element: str | None = None) -> Image.Image:
    """한 장 그린다. 같은 인자면 언제나 같은 그림이다."""
    width, height = (max(16, size[0] * SUPERSAMPLE), max(16, size[1] * SUPERSAMPLE))
    seed = _seed(entity_id)

    base = (theme.element_color(element) if element
            else theme.rarity_color(rarity if rarity is not None else 1))
    canvas = _gradient((width, height), _tint(base, 0.10),
                       _tint(base, -0.22)).convert("RGBA")

    # 무늬는 따로 그려서 살짝 흐린 뒤 얹는다 — 바탕에 녹아들어 글자를 방해하지
    # 않는다.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _emblem(ImageDraw.Draw(overlay), (width, height), seed, _tint(base, 0.34))
    canvas = Image.alpha_composite(
        canvas, overlay.filter(ImageFilter.GaussianBlur(width / 220)))

    draw = ImageDraw.Draw(canvas)
    border = theme.color("color_border")
    inset = max(2, width // 60)
    draw.rectangle([inset, inset, width - inset - 1, height - inset - 1],
                   outline=(*border, 220), width=max(2, width // 150))

    # 이름은 아래쪽 띠 위에 올린다. 무늬 위에 그냥 쓰면 읽기 어렵다.
    #
    # 띠와 글자 크기에 상한을 둔다. 비율로만 정하면 월드 배경처럼 큰 그림에서
    # 이름이 간판만 해져서, 그 위에 그려지는 지도의 칸을 덮어 버린다.
    if label:
        band = min(int(height * 0.22), int(MAX_LABEL_BAND * SUPERSAMPLE))
        draw.rectangle([0, height - band, width, height], fill=(0, 0, 0, 130))
        font = theme.font(max(10, min(band // 3,
                                      int(MAX_LABEL_FONT * SUPERSAMPLE))),
                          role="small")
        _centered(draw, label, (0, height - band, width, height), font,
                  theme.color("color_text"))

    return canvas.resize(size, Image.LANCZOS)


def _centered(draw: ImageDraw.ImageDraw, text: str, box, font,
              color: tuple[int, int, int]) -> None:
    left, top, right, bottom = box
    #: 한 줄에 안 들어가면 두 줄까지 나눈다. 그보다 길면 잘라 낸다.
    lines = [text]
    if draw.textlength(text, font=font) > (right - left) * 0.88 and len(text) > 3:
        middle = len(text) // 2
        lines = [text[:middle], text[middle:]]

    line_height = font.size + 2
    start = (top + bottom) / 2 - line_height * len(lines) / 2
    for index, line in enumerate(lines):
        width = draw.textlength(line, font=font)
        draw.text(((left + right) / 2 - width / 2, start + index * line_height),
                  line, font=font, fill=(*color, 255))


def write(library: AssetLibrary, kind: str, entity_id: str, *, label: str,
          rarity: int | None = None, element: str | None = None,
          overwrite: bool = False) -> Path | None:
    """파일로 쓴다. 이미 그림이 있으면 건드리지 않고 None 을 돌려준다."""
    if not overwrite and library.find(kind, entity_id) is not None:
        return None
    rule = library.kind(kind)
    if rule.size[0] <= 0 or rule.size[1] <= 0:
        return None                     # `ui` 처럼 크기가 정해지지 않은 종류
    path = library.expected_path(kind, entity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = generate(library.theme, size=rule.size, entity_id=entity_id,
                     label=label, rarity=rarity, element=element)
    image.save(path, "PNG", optimize=True)
    return path
