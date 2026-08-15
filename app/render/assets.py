"""직접 넣은 그림(PNG)을 찾아 쓰는 곳.

`assets/<폴더>/<id>.png` 에 파일을 두면 그 그림이 자동으로 쓰인다. 코드를
고치거나 서비스를 다시 띄울 필요가 없다 — 파일을 바꾸면 수정 시각이 달라져
다음 화면부터 새 그림이 나간다.

그림이 없거나, 크거나, 깨졌으면 **이름과 희귀도 색으로 대신 그린다**(§11).
그러니 그림을 하나도 넣지 않아도 게임은 정상 동작하고, 있는 것부터 하나씩
채워가면 된다. 어떤 그림이 아직 없는지는 `python -m app.cli.assets` 로 본다.

폴더 이름과 그릴 크기는 `config/11_에셋.toml` 이 정한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from app.render.theme import ROOT, Theme

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetKind:
    """그림 한 종류에 대한 규칙."""

    name: str
    directory: str
    size: tuple[int, int]
    #: "cover" 는 비율을 지키며 꽉 채우고 넘치는 부분을 자른다.
    #: "contain" 은 잘리지 않게 전부 넣고 남는 자리를 비운다.
    fit: str


class AssetLibrary:
    """`assets/` 아래의 그림을 찾아 열어 주는 곳."""

    def __init__(self, theme: Theme, root: Path | None = None):
        self.theme = theme
        configured = Path(theme.get("asset_root"))
        self.root = root or (configured if configured.is_absolute()
                             else ROOT / configured)
        self.extensions = list(theme.get("asset_extensions"))
        self.max_bytes = theme.int_("asset_max_bytes")
        self.max_dimension = theme.int_("asset_max_dimension")
        self._kinds = {
            name: AssetKind(name=name, directory=entry["directory"],
                            size=(int(entry["size"][0]), int(entry["size"][1])),
                            fit=entry.get("fit", "cover"))
            for name, entry in theme.get("asset_kinds").items()
        }
        #: (경로, 수정시각, 크기) → 이미지. 파일을 바꾸면 키가 달라져 다시 읽는다.
        self._cache: dict[tuple, Image.Image] = {}
        self._cache_size = theme.int_("asset_cache_size")

    # -- 찾기 ----------------------------------------------------------
    def kind(self, name: str) -> AssetKind:
        try:
            return self._kinds[name]
        except KeyError:
            raise KeyError(
                f"에셋 종류 {name!r} 은(는) config/11_에셋.toml 에 없습니다"
            ) from None

    def directory(self, kind: str) -> Path:
        return self.root / self.kind(kind).directory

    def expected_path(self, kind: str, entity_id: str) -> Path:
        """이 id 의 그림을 넣을 자리. 파일이 아직 없어도 경로를 돌려준다."""
        return self.directory(kind) / f"{entity_id}{self.extensions[0]}"

    def find(self, kind: str, entity_id: str) -> Path | None:
        """실제로 놓여 있는 파일. 없으면 None.

        확장자는 설정에 적힌 순서대로 찾으므로, 같은 이름의 png 와 jpg 가
        모두 있으면 png 가 이긴다.
        """
        directory = self.directory(kind)
        for extension in self.extensions:
            candidate = directory / f"{entity_id}{extension}"
            if candidate.is_file():
                return candidate
        return None

    # -- 열기 ----------------------------------------------------------
    def load(self, kind: str, entity_id: str,
             size: tuple[int, int] | None = None) -> Image.Image | None:
        """그림을 정해진 크기로 열어 준다. 쓸 수 없으면 None.

        None 을 돌려주는 것은 실패가 아니라 "대신 그려라"라는 뜻이다.
        """
        path = self.find(kind, entity_id)
        if path is None:
            return None

        rule = self.kind(kind)
        target = size or rule.size
        if target[0] <= 0 or target[1] <= 0:
            target = None

        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.max_bytes:
            logger.warning("에셋이 너무 큽니다(%d바이트): %s", stat.st_size, path)
            return None

        key = (str(path), stat.st_mtime_ns, target)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            with Image.open(path) as opened:
                image = opened.convert("RGBA")
        except (UnidentifiedImageError, OSError, ValueError) as error:
            logger.warning("에셋을 읽지 못했습니다: %s (%s)", path, error)
            return None

        if max(image.size) > self.max_dimension:
            image.thumbnail((self.max_dimension, self.max_dimension))
        if target:
            image = _fit(image, target, rule.fit)

        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[key] = image
        return image

    def art(self, kind: str, entity_id: str, *, label: str | None = None,
            rarity: int | None = None, element: str | None = None,
            size: tuple[int, int] | None = None) -> Image.Image:
        """언제나 그림을 돌려준다. 넣은 파일이 없으면 대신 그린다.

        `label` 을 생략하면 id 로 대신 채운다 — 그림이 없어도 뭔가는
        보여야 한다(§11). 부르는 쪽이 이름을 이미 다른 자리에 따로 그리고
        있어 그림에는 글자가 필요 없다면, `label=""` 로 명시해 끈다."""
        image = self.load(kind, entity_id, size)
        if image is not None:
            return image
        rule = self.kind(kind)
        resolved_label = entity_id if label is None else label
        return self.placeholder(size or rule.size, label=resolved_label,
                                rarity=rarity, element=element)

    def placeholder(self, size: tuple[int, int], *, label: str,
                    rarity: int | None = None,
                    element: str | None = None) -> Image.Image:
        """그림이 없을 때 대신 그리는 것 — 빗금 무늬 바탕에 이름."""
        theme = self.theme
        base = (theme.element_color(element) if element is not None
                else theme.rarity_color(rarity if rarity is not None else 1))
        image = _hatched_panel(size, base)
        draw = ImageDraw.Draw(image)

        # 넣은 그림과 구별되도록 안쪽에 테두리를 하나 그린다.
        draw.rectangle([2, 2, size[0] - 3, size[1] - 3],
                       outline=(*theme.color("color_border"), 255), width=2)
        if label:
            font = theme.font(role="small")
            _centered_wrapped(draw, label, size, font, theme.color("color_text"))
        return image

    # -- 점검 ----------------------------------------------------------
    def inventory(self, wanted: dict[str, list[tuple[str, str]]]) -> list[dict]:
        """어떤 그림이 있고 없는지.

        `wanted` 는 `{종류: [(id, 이름), …]}` 이다. 대시보드와
        `python -m app.cli.assets` 가 같은 목록을 쓴다.
        """
        report: list[dict] = []
        for kind, entries in sorted(wanted.items()):
            for entity_id, name in entries:
                path = self.find(kind, entity_id)
                report.append({
                    "kind": kind,
                    "id": entity_id,
                    "name": name,
                    "present": path is not None,
                    "path": str(path) if path else str(
                        self.expected_path(kind, entity_id)),
                    "problem": _problem(path, self.max_bytes) if path else None,
                })
        return report


def _tint(base: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """밝기만 옮긴다. 색상은 그대로 두어야 팔레트에서 벗어나지 않는다."""
    import colorsys

    hue, lightness, saturation = colorsys.rgb_to_hls(*[value / 255 for value in base])
    lightness = min(1.0, max(0.0, lightness + amount))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (int(red * 255), int(green * 255), int(blue * 255))


def _hatched_panel(size: tuple[int, int], base: tuple[int, int, int]) -> Image.Image:
    """빗금 무늬 바탕 — 카드류 화면 전체가 공유하는 기본 자리표시자 결.

    한 판 단색 대신 위아래 그러데이션에 대각선 빗금을 얹는다. 그림이 없는
    카드·초상화가 전부 같은 결로 보이도록, 이 하나만 카드가 실제로 무엇을
    그리는지와 무관하게 색만 바꿔 재사용한다."""
    width, height = size
    top, bottom = _tint(base, 0.10), _tint(base, -0.24)
    image = Image.new("RGBA", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        image.putpixel((0, y), (*tuple(
            int(top[index] + (bottom[index] - top[index]) * ratio)
            for index in range(3)), 255))
    image = image.resize((width, height), Image.BILINEAR).convert("RGBA")

    stripes = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(stripes)
    stripe_color = (*_tint(base, 0.24), 55)
    step = max(9, width // 11)
    for x in range(-height, width + height, step):
        draw.line([(x, 0), (x + height, height)], fill=stripe_color,
                  width=max(2, step // 3))
    return Image.alpha_composite(image, stripes)


def _problem(path: Path, max_bytes: int) -> str | None:
    """놓여 있긴 하지만 쓸 수 없는 파일의 이유."""
    try:
        if path.stat().st_size > max_bytes:
            return f"파일이 너무 큽니다 ({path.stat().st_size:,}바이트)"
        with Image.open(path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return "이미지로 읽을 수 없습니다"
    return None


def _fit(image: Image.Image, size: tuple[int, int], fit: str) -> Image.Image:
    """그림을 정해진 크기에 맞춘다."""
    if fit == "contain":
        copy = image.copy()
        copy.thumbnail(size)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        canvas.paste(copy, ((size[0] - copy.width) // 2,
                            (size[1] - copy.height) // 2), copy)
        return canvas

    # cover — 비율을 지키며 꽉 채우고 넘치는 부분을 가운데 기준으로 자른다.
    scale = max(size[0] / image.width, size[1] / image.height)
    scaled = image.resize((max(1, round(image.width * scale)),
                           max(1, round(image.height * scale))),
                          Image.LANCZOS)
    left = (scaled.width - size[0]) // 2
    top = (scaled.height - size[1]) // 2
    return scaled.crop((left, top, left + size[0], top + size[1]))


def _centered_wrapped(draw: ImageDraw.ImageDraw, text: str,
                      size: tuple[int, int], font, color) -> None:
    """이름을 칸 안에 줄바꿈해 가운데 놓는다."""
    width, height = size
    words = list(text)
    lines: list[str] = []
    current = ""
    for character in words:
        trial = current + character
        if draw.textlength(trial, font=font) > width - 12 and current:
            lines.append(current)
            current = character
        else:
            current = trial
    if current:
        lines.append(current)
    lines = lines[:4]

    line_height = font.size + 3 if hasattr(font, "size") else 14
    start = (height - line_height * len(lines)) // 2
    for index, line in enumerate(lines):
        text_width = draw.textlength(line, font=font)
        draw.text(((width - text_width) / 2, start + index * line_height),
                  line, font=font, fill=color)
