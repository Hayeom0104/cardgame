"""관리자 대시보드 테스트 (설계 문서 §10)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from cardgamebot.api.admin import auth
from cardgamebot.db.database import SessionLocal, engine, init_db
from cardgamebot.db.models import AdminRole, AdminUser, Base, Card, Enemy
from cardgamebot.db.seed import seed_all
from cardgamebot.main import app


def make_admin(role: AdminRole) -> AdminUser:
    with SessionLocal() as s:
        admin = AdminUser(discord_id=f"admin-{role.value}", username=f"{role.value}-님", role=role)
        s.add(admin)
        s.commit()
        s.refresh(admin)
        return admin


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as s:
        seed_all(s)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login_as(role: AdminRole = AdminRole.EDITOR) -> AdminUser:
    admin = make_admin(role)
    app.dependency_overrides[auth.current_admin] = lambda: admin
    app.dependency_overrides[auth.require_admin] = lambda: admin
    if role is AdminRole.OWNER:
        app.dependency_overrides[auth.require_owner] = lambda: admin
    return admin


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------


def test_dashboard_redirects_when_not_logged_in(client):
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


def test_login_page_explains_missing_oauth_config(client):
    body = client.get("/admin/login").text
    assert "디스코드 OAuth 가 설정되지 않았습니다" in body


def test_editor_cannot_access_user_management(client):
    login_as(AdminRole.EDITOR)
    assert client.get("/admin/users/manage").status_code == 403


def test_owner_can_access_user_management(client):
    login_as(AdminRole.OWNER)
    resp = client.get("/admin/users/manage")
    assert resp.status_code == 200
    assert "사용자 관리" in resp.text


# ---------------------------------------------------------------------------
# §10.1 콘텐츠 CRUD
# ---------------------------------------------------------------------------


def test_dashboard_lists_entity_counts(client):
    login_as()
    body = client.get("/admin/").text
    for label in ("캐릭터", "카드", "적"):
        assert label in body


@pytest.mark.parametrize("entity", ["characters", "cards", "enemies"])
def test_entity_list_and_form_render(client, entity):
    login_as()
    assert client.get(f"/admin/{entity}").status_code == 200
    assert client.get(f"/admin/{entity}/new").status_code == 200


def test_create_card_through_form(client):
    login_as()
    resp = client.post(
        "/admin/cards/new",
        data={
            "code": "test_blast",
            "name": "시험 폭발",
            "description": "테스트용 카드",
            "character_code": "",
            "kind": "attack",
            "target": "enemy_all",
            "cost": "2",
            "rarity": "4",
            "effects": '[{"op": "damage", "amount": 7}]',
            "in_gacha_pool": "on",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as s:
        card = s.query(Card).filter_by(code="test_blast").one()
        assert card.name == "시험 폭발"
        assert card.effects == [{"op": "damage", "amount": 7}]
        assert card.character_code is None      # 빈 값은 공용 카드로 저장된다
        assert card.is_starter is False         # 체크 안 한 불리언은 False


def test_invalid_json_is_rejected_with_message(client):
    login_as()
    resp = client.post(
        "/admin/cards/new",
        data={"code": "bad", "name": "불량", "kind": "attack", "target": "self",
              "cost": "1", "rarity": "1", "effects": "{이건 JSON이 아님"},
    )
    assert resp.status_code == 400
    assert "JSON 형식이 올바르지 않습니다" in resp.text


def test_duplicate_code_is_rejected(client):
    login_as()
    resp = client.post(
        "/admin/cards/new",
        data={"code": "uni_sweep", "name": "중복", "kind": "attack", "target": "self",
              "cost": "1", "rarity": "1", "effects": "[]"},
    )
    assert resp.status_code == 400
    assert "이미 존재합니다" in resp.text


def test_edit_enemy_updates_moves(client):
    login_as()
    resp = client.post(
        "/admin/enemies/goblin/edit",
        data={
            "code": "goblin", "name": "고블린 대장", "description": "",
            "tier": "elite", "hp": "60", "attack": "9", "defense": "2", "speed": "12",
            "moves": '[{"name": "강타", "weight": 1, "target": "enemy_single", '
                     '"effects": [{"op": "damage", "amount": 9}]}]',
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as s:
        enemy = s.query(Enemy).filter_by(code="goblin").one()
        assert enemy.name == "고블린 대장"
        assert enemy.hp == 60
        assert enemy.moves[0]["name"] == "강타"


def test_delete_deactivates_instead_of_removing(client):
    """진행 중인 런이 참조할 수 있어 물리 삭제하지 않는다."""
    login_as()
    resp = client.post("/admin/cards/uni_sweep/delete", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as s:
        card = s.query(Card).filter_by(code="uni_sweep").one()
        assert card.is_active is False


# ---------------------------------------------------------------------------
# §10.2 이미지 업로드
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 40, 40)).save(buf, format="PNG")
    return buf.getvalue()


def test_image_upload_is_stored_and_linked(client):
    login_as()
    resp = client.post(
        "/admin/cards/new",
        data={"code": "art_card", "name": "아트 카드", "kind": "attack",
              "target": "enemy_single", "cost": "1", "rarity": "1", "effects": "[]"},
        files={"image": ("art.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as s:
        card = s.query(Card).filter_by(code="art_card").one()
        assert card.art_path and card.art_path.endswith(".png")

    # 업로드된 파일이 정적 경로로 서빙되어 렌더러가 읽을 수 있어야 한다.
    assert client.get(f"/uploads/{card.art_path}").status_code == 200


def test_non_image_upload_is_rejected(client):
    login_as()
    resp = client.post(
        "/admin/cards/new",
        data={"code": "bad_art", "name": "나쁜 아트", "kind": "attack",
              "target": "enemy_single", "cost": "1", "rarity": "1", "effects": "[]"},
        files={"image": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
    )
    assert resp.status_code == 400
    assert "지원하지 않는 이미지 형식" in resp.text
