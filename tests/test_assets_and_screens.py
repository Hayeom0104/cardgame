"""에셋과 화면 렌더 — 그림을 넣으면 쓰이고, 없으면 대신 그린다.

§11의 약속은 "화면이 실패하는 일은 없다"는 것이다. 그림이 없어도, 깨져도,
너무 커도 화면은 나가야 한다.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.render import panels
from app.render import theme as theme_module
from app.render.assets import AssetLibrary


@pytest.fixture
def library(tmp_path):
    """빈 에셋 폴더를 가진 라이브러리 — 파일을 하나씩 넣어 보며 확인한다."""
    theme = theme_module.load()
    lib = AssetLibrary(theme, root=tmp_path)
    for kind in theme.get("asset_kinds"):
        lib.directory(kind).mkdir(parents=True, exist_ok=True)
    return lib


def _png(path, size=(400, 560), color=(180, 60, 40)):
    Image.new("RGB", size, color).save(path)
    return path


def _decode(attachment) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(attachment.data_b64)))


CARD = {"card_id": "card_화_강타", "name": "화염 강타", "cost": 2, "element": "화",
        "rarity_tier": 4, "category": "공격", "upgrade_tier": 1}


# =====================================================================
# 그림 찾기
# =====================================================================
def test_a_png_that_was_added_is_actually_used(library):
    """파일을 두면 그 그림이 쓰인다 — 이게 전부여야 한다."""
    _png(library.directory("card") / "card_화_강타.png")
    image = library.load("card", "card_화_강타")
    assert image is not None
    assert image.size == library.kind("card").size


def test_replacing_the_file_replaces_the_image(library):
    """같은 이름으로 덮어쓰면 다음부터 새 그림이 나간다.

    캐시가 파일 수정 시각을 보지 않으면 옛 그림이 계속 나가 버린다.
    """
    path = library.directory("card") / "card_화_강타.png"
    _png(path, color=(200, 0, 0))
    first = library.load("card", "card_화_강타").getpixel((90, 20))

    import os
    _png(path, color=(0, 0, 200))
    os.utime(path, (0, 0))                      # 수정 시각을 확실히 바꾼다
    second = library.load("card", "card_화_강타").getpixel((90, 20))
    assert first != second


def test_png_wins_over_other_extensions(library):
    """같은 이름의 png 와 jpg 가 있으면 png 를 쓴다."""
    _png(library.directory("card") / "card_화_강타.png")
    Image.new("RGB", (10, 10)).save(library.directory("card") / "card_화_강타.jpg")
    assert library.find("card", "card_화_강타").suffix == ".png"


def test_a_missing_image_is_not_an_error(library):
    """없는 것은 실패가 아니다 — 대신 그리라는 뜻이다."""
    assert library.load("card", "없는카드") is None
    art = library.art("card", "없는카드", label="없는 카드", rarity=3)
    assert art.size == library.kind("card").size


def test_a_broken_file_falls_back_instead_of_crashing(library):
    """이미지가 아닌 파일이 놓여 있어도 화면은 나가야 한다 (§11)."""
    (library.directory("card") / "card_화_강타.png").write_bytes(
        "이건 그림이 아니다".encode("utf-8"))
    assert library.load("card", "card_화_강타") is None
    assert library.art("card", "card_화_강타", label="화염 강타").size

    report = library.inventory({"card": [("card_화_강타", "화염 강타")]})
    assert report[0]["present"] and report[0]["problem"]


def test_an_oversized_file_is_refused(library, monkeypatch):
    _png(library.directory("card") / "card_화_강타.png")
    monkeypatch.setattr(library, "max_bytes", 10)
    assert library.load("card", "card_화_강타") is None


def test_the_inventory_tells_you_where_to_put_the_file(library):
    report = library.inventory({"card": [("card_새카드", "새 카드")]})
    assert report[0]["present"] is False
    assert report[0]["path"].endswith("card_새카드.png")


def test_an_unknown_asset_kind_is_refused(library):
    with pytest.raises(KeyError, match="11_에셋"):
        library.directory("존재하지않는종류")


# =====================================================================
# 화면
# =====================================================================
def test_every_screen_renders_within_the_attachment_limits():
    """§1.3.7 — 4 MiB, 4096×4096 안에 들어야 중앙봇이 받아준다.

    `to_attachment` 가 이미 검사하지만, 화면마다 실제로 통과하는지는 별개다.
    """
    cards = [dict(CARD, card_id=f"card_{i}") for i in range(6)]
    party = [{"character_id": "char_ignis", "name": "이그니스", "star_rank": 3,
              "element": "화", "job_role": "공격형", "hp": 82, "atk": 16,
              "def": 6, "spd": 105}]
    nodes = [{"node_index": i, "depth": i // 2 + 1, "node_type": "전투"}
             for i in range(8)]

    screens = [
        panels.render_map(nodes, [(0, 1)], current_node_index=0, available={1}),
        panels.render_shop([dict(cards[0], kind="card", price=80)], currency=100),
        panels.render_banner({"banner_id": "b1", "name": "배너"},
                             rates={"최고": 0.015}, carta=100),
        panels.render_gacha_results([{"kind": "card", "entity_id": "card_0",
                                      "name": "카드", "rarity_tier": 4}] * 10),
        panels.render_prep(party, world="세계 1", deck=cards),
        panels.render_settlement({"inventory": {}, "rewards": {}}),
    ]
    for attachment in screens:
        attachment.validate()
        assert attachment.decoded_size > 0


def test_the_battle_screen_is_two_panels_plus_the_hand():
    """§1.3.7 두 패널 규칙. 손패를 넘기면 한 장이 더 붙는다."""
    ally = [{"character_id": "c", "name": "아군", "hp_current": 10, "hp_max": 20,
             "statuses": []}]
    enemy = [{"battle_unit_id": 1, "enemy_id": "e", "name": "적",
              "hp_current": 5, "hp_max": 10}]
    assert len(panels.render_battle_screen(ally, enemy, {}, resource=3,
                                           round_no=1)) == 2
    with_hand = panels.render_battle_screen(ally, enemy, {}, resource=3,
                                            round_no=1, hand=[CARD])
    assert len(with_hand) == 3
    assert with_hand[2].filename == "deckout_hand.png"


def test_a_sold_out_item_stays_on_the_shelf():
    """산 물건을 목록에서 빼면 다시 왔을 때 무엇이 있었는지 알 수 없다."""
    items = [dict(CARD, kind="card", price=80, purchased=True),
             dict(CARD, kind="card", price=40)]
    image = _decode(panels.render_shop(items, currency=100))
    assert image.size == tuple(theme_module.load().size("shop_size"))


def test_an_unaffordable_card_is_dimmed_not_hidden():
    """살 수 없는 카드도 보여야 한다 — 무엇을 놓치는지 알아야 하기 때문이다."""
    theme = theme_module.load()
    canvas = panels.Canvas(theme.size("card_size"), theme)
    bright = panels.render_card(CARD, canvas)
    dim = panels.render_card(CARD, canvas, dimmed=True)
    assert bright.size == dim.size
    assert sum(bright.getpixel((90, 20))[:3]) > sum(dim.getpixel((90, 20))[:3])


def test_the_screen_sizes_come_from_the_config(monkeypatch):
    """`config/10_화면.toml` 의 크기를 바꾸면 화면 크기가 따라 바뀐다."""
    theme = theme_module.load()
    monkeypatch.setitem(theme._values, "shop_size", [400, 300])
    monkeypatch.setattr(theme_module, "load", lambda: theme)
    image = _decode(panels.render_shop([], currency=0))
    assert image.size == (400, 300)


def test_the_font_falls_back_without_crashing(monkeypatch):
    """글꼴이 하나도 없어도 화면은 나가야 한다 (§11)."""
    theme = theme_module.load()
    monkeypatch.setitem(theme._values, "font_candidates", ["/없는/글꼴.ttf"])
    assert theme.font(role="body") is not None


def test_a_font_without_hangul_is_passed_over_when_one_exists():
    """설치돼 있다는 것과 한글이 그려진다는 것은 다르다.

    한글 글리프가 없는 글꼴을 고르면 이름이 전부 네모로 나온다.
    """
    from app.render.theme import _supports_hangul

    theme = theme_module.load()
    font = theme.font(role="body")
    candidates = [path for path in theme.get("font_candidates")]
    import pathlib
    available = [p for p in candidates
                 if pathlib.Path(p).exists()
                 or (theme_module.ROOT / p).exists()]
    if not available:
        pytest.skip("이 환경에는 글꼴 후보가 하나도 없습니다")
    # 후보 중 한글이 되는 것이 있다면, 고른 글꼴도 한글이 되어야 한다.
    from PIL import ImageFont
    any_hangul = False
    for path in available:
        resolved = pathlib.Path(path)
        if not resolved.is_absolute():
            resolved = theme_module.ROOT / resolved
        try:
            any_hangul |= _supports_hangul(ImageFont.truetype(str(resolved), 18))
        except OSError:
            continue
    if any_hangul:
        assert _supports_hangul(font)


# =====================================================================
# 핸들러에 실제로 붙는가
# =====================================================================
def test_a_real_battle_response_carries_the_panels(db, balance, version, user_id):
    """전투 화면 응답에 그림이 실제로 실려 나가야 한다.

    렌더러가 있는 것과 화면에 붙는 것은 다르다.
    """
    from app.api import visuals
    from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
    from app.engine import battle as bt
    from app.engine import encounter as enc
    from app.engine import lifecycle as lc
    from app.engine.rng import JournaledRng

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=0,
                                  encounter_id="enc_tut_2",
                                  content_version_id=version)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                             content_version_id=version,
                             rng=JournaledRng(db, run_id, run["rng_seed"]))
    engine.start()
    engine.advance()

    art = visuals.battle(db, balance, battle_id=battle_id, run=run)
    assert [entry["filename"] for entry in art][:2] == ["deckout_ally.png",
                                                        "deckout_enemy.png"]
    for entry in art:
        assert base64.b64decode(entry["data_b64"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_map_response_carries_the_map(db, balance, version, user_id):
    from app.api import visuals
    from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
    from app.engine import lifecycle as lc

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    art = visuals.game_map(db, run, available={0})
    assert len(art) == 1 and art[0]["filename"] == "deckout_map.png"


def test_a_render_failure_does_not_break_the_players_action(caplog):
    """§16.7 — 여기서 예외가 새면 조작이 되돌아가고 중앙봇이 무한 재전송한다."""
    from app.api import visuals

    def explode():
        raise RuntimeError("렌더가 터졌다")

    assert visuals.safely(explode) == []
