"""Outbound Central Economy API contract tests."""

from __future__ import annotations

import asyncio

import pytest

from cardgamebot.core.central_client import CentralAPIError, CentralBotClient


class _Response:
    content = b"{}"

    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _Client:
    captured: dict = {}
    payload: dict = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, **kwargs):
        type(self).captured = {"method": method, "url": url, **kwargs}
        return _Response(type(self).payload)


def _live_client(monkeypatch, payload: dict) -> CentralBotClient:
    import cardgamebot.core.central_client as central_module

    _Client.payload = payload
    _Client.captured = {}
    monkeypatch.setattr(central_module.httpx, "AsyncClient", _Client)
    client = CentralBotClient(base_url="http://central.test", api_key="issued-secret")
    client.enabled = True
    return client


def test_currency_call_uses_api_key_idempotency_and_applied_delta(monkeypatch):
    client = _live_client(monkeypatch, {"amount": -150, "value_after": 50, "replayed": False})

    result = asyncio.run(client.spend_currency(
        "123456789012345678", 150, idempotency_key="deckout:shop:42", reason="item_purchase"
    ))

    assert result == 50
    assert _Client.captured["method"] == "POST"
    assert _Client.captured["url"] == "http://central.test/v1/currency/add"
    assert _Client.captured["headers"]["X-API-Key"] == "issued-secret"
    assert _Client.captured["json"] == {
        "user_id": 123456789012345678,
        "amount": -150,
        "idempotency_key": "deckout:shop:42",
        "reason": "item_purchase",
    }


def test_partial_central_deduction_is_rejected(monkeypatch):
    client = _live_client(monkeypatch, {"amount": -30, "value_after": 0})

    with pytest.raises(CentralAPIError, match="모두 적용하지"):
        asyncio.run(client.spend_currency(
            "123456789012345678", 150, idempotency_key="deckout:shop:43", reason="item_purchase"
        ))
