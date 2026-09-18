"""프로필 사진 — 허브 초상으로 쓰는 원격 그림 (오너 요청).

연동 가이드(2026-09-11) §7 확인 결과 아바타 URL은 `/event.avatar_url`에서
오고 `app.content.seed.remember_identity()`가 계정에 적어 둔다 — 이 모듈은
그 URL이 주어졌을 때 그림을 실제로 받아 오는 부분만 한다. 못 받아도 화면은
항상 나가야 한다(§11) — 모든 실패 경로가 None을 돌려주는지가 이 파일이
확인하는 전부다.
"""

from __future__ import annotations

import io

import httpx
from PIL import Image

from app.central import avatar as av


# =====================================================================
# fetch_avatar — 실패는 전부 None
# =====================================================================
def _png_bytes(size=(64, 64), color=(200, 100, 50, 255)) -> bytes:
    image = Image.new("RGBA", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=None, response=self)


def test_fetch_avatar_opens_a_valid_image(monkeypatch):
    monkeypatch.setattr(av.httpx, "get", lambda *a, **k: _FakeResponse(_png_bytes()))
    image = av.fetch_avatar("https://cdn.example/a.png")
    assert image is not None
    assert image.mode == "RGBA"
    assert image.size == (64, 64)


def test_fetch_avatar_returns_none_on_http_error(monkeypatch):
    def raise_error(*args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(av.httpx, "get", raise_error)
    assert av.fetch_avatar("https://cdn.example/a.png") is None


def test_fetch_avatar_returns_none_on_bad_status(monkeypatch):
    monkeypatch.setattr(av.httpx, "get",
                        lambda *a, **k: _FakeResponse(b"", status=404))
    assert av.fetch_avatar("https://cdn.example/missing.png") is None


def test_fetch_avatar_returns_none_for_oversized_content(monkeypatch):
    huge = b"\x00" * (av.MAX_BYTES + 1)
    monkeypatch.setattr(av.httpx, "get", lambda *a, **k: _FakeResponse(huge))
    assert av.fetch_avatar("https://cdn.example/huge.png") is None


def test_fetch_avatar_returns_none_for_non_image_content(monkeypatch):
    monkeypatch.setattr(av.httpx, "get",
                        lambda *a, **k: _FakeResponse(b"<html>not an image</html>"))
    assert av.fetch_avatar("https://cdn.example/a.png") is None


def test_fetch_avatar_returns_none_for_empty_content(monkeypatch):
    monkeypatch.setattr(av.httpx, "get", lambda *a, **k: _FakeResponse(b""))
    assert av.fetch_avatar("https://cdn.example/a.png") is None
