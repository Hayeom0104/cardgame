"""Shared fixtures: an in-memory database seeded with published content."""

from __future__ import annotations

import pytest

from app.content.balance import Balance
from app.content.seed import create_account, seed_all
from app.db.connection import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "deckout.db")
    database.migrate()
    return database


@pytest.fixture
def version(db) -> int:
    return seed_all(db)


@pytest.fixture
def balance(db, version) -> Balance:
    return Balance(db, version)


@pytest.fixture
def user_id(db, version) -> int:
    uid = 424242
    create_account(db, uid, version)
    return uid
