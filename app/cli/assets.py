"""넣어야 할 그림이 무엇이고 무엇이 아직 없는지 확인한다.

    python -m app.cli.assets              # 전체 목록
    python -m app.cli.assets 없음          # 아직 안 넣은 것만
    python -m app.cli.assets 문제          # 넣긴 했는데 못 쓰는 것만
    python -m app.cli.assets card         # 카드만
    python -m app.cli.assets --폴더만들기   # 필요한 폴더를 미리 만들어 둔다

그림을 넣는 방법은 간단하다. 출력에 나오는 경로 그대로 파일을 두면 된다.

    assets/cards/card_화염구.png

파일을 두면 다음 화면부터 바로 쓰인다. 서비스를 다시 띄우지 않아도 된다.
그림이 없는 것은 이름과 색으로 대신 그리므로, 하나도 없어도 게임은 돌아간다.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.content.versioning import current_version_id
from app.db.connection import Database
from app.render.assets import AssetLibrary
from app.render.theme import load as load_theme

#: 콘텐츠에서 그림이 필요한 것들. (에셋 종류, 테이블, id 열, 이름 열)
CONTENT_SOURCES = (
    ("card", "cards", "card_id", "name"),
    ("character", "characters", "character_id", "name"),
    ("enemy", "enemies", "enemy_id", "name"),
    ("banner", "banners", "banner_id", "name"),
    ("world", "worlds", "world_id", "name"),
    ("equipment", "equipment_defs", "equipment_def_id", "name"),
    ("status", "statuses", "status_id", "name"),
)


def wanted(db: Database, content_version_id: int) -> dict[str, list[tuple[str, str]]]:
    """콘텐츠에 있는 것 전부 — 이 목록이 곧 넣을 수 있는 그림의 목록이다."""
    result: dict[str, list[tuple[str, str]]] = {}
    for kind, table, id_column, name_column in CONTENT_SOURCES:
        try:
            rows = db.query(
                f"SELECT {id_column} AS id, {name_column} AS name FROM {table} "
                f"WHERE content_version_id = ? ORDER BY {id_column}",
                (content_version_id,))
        except Exception:                       # 아직 없는 테이블은 건너뛴다
            continue
        result[kind] = [(row["id"], row["name"]) for row in rows]
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    library = AssetLibrary(load_theme())

    if "--폴더만들기" in argv:
        for kind in library.theme.get("asset_kinds"):
            library.directory(kind).mkdir(parents=True, exist_ok=True)
        print(f"{library.root} 아래에 폴더를 준비했습니다.")
        return 0

    needle = next((arg for arg in argv if not arg.startswith("--")), "")

    db = Database(settings.database_path)
    db.migrate()
    version = current_version_id(db)
    if version is None:
        print("발행된 콘텐츠 버전이 없습니다. 먼저 python -m app.cli.bootstrap 을 실행하세요.")
        return 1

    report = library.inventory(wanted(db, version))

    only_missing = needle == "없음"
    only_broken = needle == "문제"
    if only_missing:
        report = [row for row in report if not row["present"]]
    elif only_broken:
        report = [row for row in report if row["present"] and row["problem"]]
    elif needle:
        report = [row for row in report
                  if needle in row["kind"] or needle in row["id"]
                  or needle in (row["name"] or "")]

    if not report:
        print("해당하는 항목이 없습니다." if needle else "콘텐츠가 비어 있습니다.")
        return 0

    current = None
    for row in report:
        if row["kind"] != current:
            current = row["kind"]
            folder = library.directory(current)
            print(f"\n── {current}  ({folder}) " + "─" * 20)
        if row["problem"]:
            mark = f"✗ {row['problem']}"
        elif row["present"]:
            mark = "○ 있음"
        else:
            mark = "· 없음"
        print(f"  {mark:28} {row['id']}   {row['name']}")

    present = sum(1 for row in report if row["present"] and not row["problem"])
    broken = sum(1 for row in report if row["problem"])
    print(f"\n총 {len(report)}개 · 넣은 것 {present}개 · "
          f"아직 없는 것 {len(report) - present - broken}개 · 못 쓰는 것 {broken}개")
    if broken:
        print("못 쓰는 파일은 그림 대신 이름과 색으로 그려집니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
