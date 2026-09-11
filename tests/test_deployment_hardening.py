"""배포 환경에서만 드러나는 설정·파일 경로 회귀 검사."""

from __future__ import annotations

import tomllib
from pathlib import Path

from app.db.connection import Database


def test_nested_sqlite_parent_is_created(tmp_path):
    path = tmp_path / "runtime" / "data" / "deckout.db"
    db = Database(path)
    try:
        db.migrate()
        assert path.is_file()
    finally:
        db.close()


def test_runtime_dependencies_and_package_scope_are_declared():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    assert any(item.startswith("jinja2") for item in dependencies)
    assert any(item.startswith("python-dotenv") for item in dependencies)
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == ["app*"]


def test_tutorial_slime_defense_matches_the_lowered_stat_block(db, version):
    row = db.one(
        "SELECT def FROM enemies WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_tut_슬라임'", (version,))
    assert row is not None
    assert row["def"] == 1
