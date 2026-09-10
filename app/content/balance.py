"""§15 밸런싱 상수 — 값의 원본은 `config/*.toml` 이다.

> §15의 모든 수치는 설정/DB에서 읽으며 대시보드에서 편집할 수 있다.
> **어느 것도 코드에 하드코딩하지 않는다.**

값은 `config/` 아래에 게임 플레이 영역별로 나뉘어 있고(전투, 뽑기, 경제 …),
설정마다 무엇을 바꾸는 값인지 설명이 주석으로 붙어 있다. 자세한 내용은
`config/README.md` 를 보라.

엔진은 이 모듈의 로딩 경로를 거치지 않는다. 언제나 `Balance` 를 통해 DB에서
읽으며, `Balance` 는 콘텐츠 버전에 고정되어 있으므로 진행 중인 런은 시작할 때의
수치를 그대로 유지한다 (§10.6) — 파일을 고쳐도 돌고 있는 런은 흔들리지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

from app.content import config_loader
from app.db.connection import Database


def _load_defaults() -> dict[str, Any]:
    """`config/*.toml` 을 읽어 새 데이터베이스에 심을 기본값을 만든다."""
    return config_loader.load_constants()


#: 새 콘텐츠 버전을 만들 때 심는 §15 기본값. 파일이 원본이며, 이 이름은
#: 예전 코드와의 호환을 위해 남겨둔 것이다.
DEFAULT_CONSTANTS: dict[str, Any] = _load_defaults()


class Balance:
    """Reads §15 constants for one pinned content version.

    Values are cached per instance: a run's numbers cannot shift underneath it
    mid-battle even if the dashboard publishes during play.
    """

    def __init__(self, db: Database, content_version_id: int):
        self.db = db
        self.content_version_id = content_version_id
        self._cache: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        row = self.db.one(
            "SELECT value_json FROM balancing_constants "
            "WHERE content_version_id = ? AND key = ?",
            (self.content_version_id, key),
        )
        if row is None:
            if default is not None:
                return default
            raise KeyError(
                f"balancing constant {key!r} is not defined at content version "
                f"{self.content_version_id} — §15 values are never hardcoded, so a "
                "missing key is a content error"
            )
        value = json.loads(row["value_json"])
        self._cache[key] = value
        return value

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def int_(self, key: str) -> int:
        return int(self.get(key))

    def float_(self, key: str) -> float:
        return float(self.get(key))


def seed_constants(db: Database, content_version_id: int) -> None:
    """설정 파일의 값과 Deckout 기본 확장 콘텐츠를 새 버전에 심는다."""
    with db.tx() as conn:
        for key, value in DEFAULT_CONSTANTS.items():
            conn.execute(
                "INSERT INTO balancing_constants (content_version_id, key, value_json) "
                "VALUES (?, ?, ?) ON CONFLICT(content_version_id, key) DO UPDATE SET "
                "value_json = excluded.value_json",
                (content_version_id, key, json.dumps(value, ensure_ascii=False)),
            )

    # 확장팩 데이터는 기존 seed 함수들이 실행되기 전에 넣어도 안전하다.
    # JSON 내부 참조는 seed_all의 최종 publish 검증 시점에 확인되고,
    # INSERT OR REPLACE라 개발 DB 초기화를 반복해도 중복되지 않는다.
    from app.content.expansion_v1 import seed_expansion

    card_cost_min = int(DEFAULT_CONSTANTS.get("card_cost_min", 1))
    seed_expansion(db, content_version_id, card_cost_min=card_cost_min)
    seed_metadata(db)


def seed_metadata(db: Database) -> None:
    """설정 설명을 파일에서 읽어 DB에 새로 쓴다.

    설명은 값이 아니라 키에 대한 것이라 콘텐츠 버전에 묶이지 않는다. 파일이
    언제나 원본이므로, 파일에서 사라진 설명은 DB에서도 지운다.
    """
    docs = config_loader.load_docs()
    origins = config_loader.source_files()
    with db.tx() as conn:
        conn.execute("DELETE FROM balancing_metadata")
        for key, description in sorted(docs.items()):
            conn.execute(
                "INSERT INTO balancing_metadata (key, source_file, description) "
                "VALUES (?, ?, ?)",
                (key, origins.get(key.split(".")[0], ""), description),
            )


def describe(db: Database, key: str) -> str | None:
    """설정 하나의 설명. 대시보드가 설정 옆에 그대로 보여줄 수 있다."""
    row = db.one("SELECT description FROM balancing_metadata WHERE key = ?", (key,))
    return row["description"] if row else None
