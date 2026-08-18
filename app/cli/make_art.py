"""콘텐츠마다 자리를 지킬 그림을 만든다.

    python -m app.cli.make_art                # 아직 그림이 없는 것만
    python -m app.cli.make_art --덮어쓰기      # 만들어 둔 것까지 다시
    python -m app.cli.make_art --종류 card    # 카드만

**직접 넣은 그림은 절대 덮지 않는다.** `--덮어쓰기` 를 주더라도, 이 명령이
만든 파일인지 아닌지는 구별하지 않으므로 진짜 아트를 넣은 뒤에는 종류를
좁혀서 쓰는 편이 안전하다.

만들어진 그림은 id 마다 다르게 생겼다 — 같은 id 는 언제나 같은 모양이고
다른 id 는 확실히 다르다. 최종 아트가 나오면 같은 이름으로 덮으면 되고,
다음 화면부터 바로 반영된다.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.content.versioning import current_version_id
from app.db.connection import Database
from app.render import artgen
from app.render.assets import AssetLibrary
from app.render.theme import load as load_theme

#: (에셋 종류, 테이블, id 열, 이름 열, 희귀도 열, 원소 열)
#: 희귀도와 원소가 없는 종류는 None — 그때는 희귀도 1의 색으로 그린다.
SOURCES = (
    ("card", "cards", "card_id", "name", "rarity_tier", "element"),
    ("passive", "passive_cards", "passive_card_id", "name", "rarity_tier", None),
    ("character", "characters", "character_id", "name", "base_rarity", "element"),
    ("enemy", "enemies", "enemy_id", "name", None, "element"),
    ("equipment", "equipment_defs", "equipment_def_id", "name", None, None),
    ("world", "worlds", "world_id", "name", None, None),
    ("status", "statuses", "status_id", "name", None, None),
    # 배너에는 이름 열이 없다. id 를 그대로 이름으로 쓴다.
    ("banner", "banners", "banner_id", "banner_id", None, None),
)

#: 이 종류는 화면 쪽(카드·아군·적 패널·배너)이 이름을 그림 위에 직접
#: 얹으므로, 그림에마저 이름 띠를 구우면 글자가 겹친다. 나머지 종류는
#: 이름을 따로 그리지 않는 화면에서도 쓰이므로 그림에 이름을 남겨 둔다.
_NO_BAKED_LABEL = {"card", "character", "enemy", "banner"}


def rows_for(db: Database, version: int, source) -> list[dict]:
    kind, table, id_column, name_column, rarity_column, element_column = source
    columns = [f"{id_column} AS id", f"{name_column} AS name"]
    columns.append(f"{rarity_column} AS rarity" if rarity_column else "NULL AS rarity")
    columns.append(f"{element_column} AS element" if element_column
                   else "NULL AS element")
    rows = db.query(
        f"SELECT {', '.join(columns)} FROM {table} WHERE content_version_id = ? "
        f"ORDER BY {id_column}", (version,))
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    overwrite = "--덮어쓰기" in argv or "--overwrite" in argv

    only = None
    for flag in ("--종류", "--kind"):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                only = argv[index + 1]

    db = Database(settings.database_path)
    db.migrate()
    version = current_version_id(db)
    if version is None:
        print("발행된 콘텐츠 버전이 없습니다. "
              "먼저 python -m app.cli.bootstrap 을 실행하세요.")
        return 1

    library = AssetLibrary(load_theme())
    made = skipped = 0
    for source in SOURCES:
        kind = source[0]
        if only and only != kind:
            continue
        try:
            rows = rows_for(db, version, source)
        except Exception as error:                  # 아직 없는 테이블
            print(f"── {kind}: 건너뜀 ({error})")
            continue
        if not rows:
            continue

        print(f"\n── {kind}  ({library.directory(kind)})")
        for row in rows:
            label = "" if kind in _NO_BAKED_LABEL else (row["name"] or row["id"])
            path = artgen.write(
                library, kind, row["id"], label=label,
                rarity=row["rarity"], element=row["element"],
                overwrite=overwrite)
            if path is None:
                skipped += 1
            else:
                made += 1
                print(f"  + {path.name}")

    print(f"\n{made}장 만들었습니다. "
          f"이미 있어서 건드리지 않은 것 {skipped}장.")
    if not overwrite and skipped:
        print("다시 만들려면 --덮어쓰기 를 붙이세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
