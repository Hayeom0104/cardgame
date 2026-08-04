from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 설정 캐시가 잡히기 전에 테스트용 경로를 주입해야 한다.
_TMP = Path(tempfile.mkdtemp(prefix="cardgame-test-"))
os.environ.setdefault("CARDGAME_DB_PATH", str(_TMP / "test.db"))
os.environ.setdefault("CARDGAME_UPLOAD_DIR", str(_TMP / "uploads"))
os.environ.setdefault("CARDGAME_RENDER_CACHE_DIR", str(_TMP / "cache"))

from cardgamebot.db.database import SessionLocal, engine  # noqa: E402
from cardgamebot.db.models import Base  # noqa: E402
from cardgamebot.db.seed import seed_all  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    seed_all(s)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def user(session):
    from cardgamebot.core.commands import get_or_create_user

    u = get_or_create_user(session, "test-user-1", "테스터")
    session.commit()
    return u
