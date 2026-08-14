"""자리를 지킬 그림 만들기 — 같은 id 는 같게, 다른 id 는 다르게."""

from __future__ import annotations

from PIL import Image

from app.cli import make_art
from app.render import artgen
from app.render.assets import AssetLibrary
from app.render.theme import load as load_theme


def library(tmp_path) -> AssetLibrary:
    return AssetLibrary(load_theme(), root=tmp_path)


def test_the_same_id_always_gives_the_same_picture():
    theme = load_theme()
    first = artgen.generate(theme, size=(60, 80), entity_id="card_가", label="가")
    second = artgen.generate(theme, size=(60, 80), entity_id="card_가", label="가")
    assert first.tobytes() == second.tobytes()


def test_different_ids_look_different():
    """이것이 §11의 단색 대체 그림과 다른 점이다 — 생김새로 구별할 수 있다."""
    theme = load_theme()
    first = artgen.generate(theme, size=(60, 80), entity_id="card_가", label="같은 이름")
    second = artgen.generate(theme, size=(60, 80), entity_id="card_나", label="같은 이름")
    assert first.tobytes() != second.tobytes()


def test_the_element_decides_the_colour():
    theme = load_theme()
    fire = artgen.generate(theme, size=(40, 40), entity_id="x", label="",
                           element="화")
    water = artgen.generate(theme, size=(40, 40), entity_id="x", label="",
                            element="수")
    assert fire.getpixel((20, 4)) != water.getpixel((20, 4))


def test_it_writes_a_readable_png(tmp_path):
    lib = library(tmp_path)
    path = artgen.write(lib, "card", "card_시험", label="시험 카드", rarity=3)
    assert path is not None and path.is_file()
    with Image.open(path) as opened:
        assert opened.size == lib.kind("card").size
    # 라이브러리가 실제로 이 파일을 찾아 쓴다.
    assert lib.find("card", "card_시험") == path


def test_it_never_overwrites_art_that_is_already_there(tmp_path):
    lib = library(tmp_path)
    path = artgen.write(lib, "card", "card_시험", label="시험", rarity=1)
    original = path.read_bytes()

    assert artgen.write(lib, "card", "card_시험", label="다른 이름", rarity=6) is None
    assert path.read_bytes() == original


def test_overwrite_is_opt_in(tmp_path):
    lib = library(tmp_path)
    path = artgen.write(lib, "card", "card_시험", label="시험", rarity=1)
    original = path.read_bytes()

    again = artgen.write(lib, "card", "card_시험", label="시험", rarity=6,
                         overwrite=True)
    assert again == path
    assert path.read_bytes() != original


def test_a_kind_with_no_fixed_size_is_skipped(tmp_path):
    """`ui` 는 크기가 정해져 있지 않다 — 무엇을 그릴지 알 수 없으므로 만들지 않는다."""
    assert artgen.write(library(tmp_path), "ui", "frame", label="틀") is None


def test_every_source_table_can_actually_be_queried(db, version):
    """열 이름이 틀리면 여기서 걸린다 — 조용히 건너뛰면 그 종류만 그림이 없다."""
    for source in make_art.SOURCES:
        rows = make_art.rows_for(db, version, source)
        assert isinstance(rows, list)


def test_the_seeded_content_all_gets_a_picture(db, version, tmp_path):
    lib = library(tmp_path)
    for source in make_art.SOURCES:
        kind = source[0]
        for row in make_art.rows_for(db, version, source):
            assert artgen.write(lib, kind, row["id"],
                                label=row["name"] or row["id"],
                                rarity=row["rarity"],
                                element=row["element"]) is not None
