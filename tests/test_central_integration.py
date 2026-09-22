from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app.api import server, events, handlers
from app.config import settings


@pytest.fixture
def secured(monkeypatch, db, version):
    monkeypatch.setattr(settings, 'ingress_secret', 'integration-test-secret')
    monkeypatch.setattr(settings, 'allow_unauthenticated_local', False)
    monkeypatch.setattr(server, 'state', {'db': db, 'content_version_id': version, 'accepting': True})
    return TestClient(server.app)


@pytest.mark.parametrize('kind', list(events.INBOUND_TYPES))
def test_all_events_require_auth(secured, kind):
    assert secured.post('/event', json={'type': kind}).status_code == 401
    assert server.state['accepting'] is True


def test_auth_precedes_parsing_and_shutdown(secured):
    assert secured.post('/event', content='invalid').status_code == 401
    assert secured.post('/shutdown', json={'timeout': 30}).status_code == 401
    assert server.state['accepting'] is True
    assert secured.post('/event', json={'type': 'unknown'}, headers={
        'X-ARI-Minigame-Secret': 'wrong'}).status_code == 401
    response = secured.post('/event', json={'type': 'unknown'}, headers={
        'X-ARI-Minigame-Secret': 'integration-test-secret'})
    assert response.status_code == 200
    assert response.json()['action'] == 'ignore'


def test_missing_secret_fails_closed(secured, monkeypatch):
    monkeypatch.setattr(settings, 'ingress_secret', '')
    assert secured.post('/event', json={}).status_code == 401
    with pytest.raises(RuntimeError, match='INGRESS_SECRET'):
        with TestClient(server.app):
            pass


def test_dev_bypass_cannot_override_configured_secret(secured, monkeypatch):
    monkeypatch.setattr(settings, 'allow_unauthenticated_local', True)
    assert secured.post('/shutdown', json={}).status_code == 401


def test_health_readiness(secured, monkeypatch):
    assert secured.get('/healthz').status_code == 200
    server.state['accepting'] = False
    assert secured.get('/healthz').status_code == 503
    server.state['accepting'] = True
    monkeypatch.setattr(server.state['db'], 'one', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert secured.get('/healthz').status_code == 503


@pytest.mark.parametrize('kind', ['message', 'interaction', 'modal_submit'])
def test_event_identity_and_selection(kind):
    event = events.parse_event({'type': kind, 'user_id': 42, 'custom_id': 'dko:x',
        'username': '테스트', 'avatar_url': 'https://cdn.discordapp.com/avatars/42/a.png',
        'values': ['b', 'a', 'b']})
    assert event.username == '테스트'
    assert event.avatar_url == 'https://cdn.discordapp.com/avatars/42/a.png'
    if kind == 'interaction':
        assert event.component_type == 'button'
        assert event.values == ['b', 'a', 'b']


def test_preflight_missing_settings_does_not_call_network(monkeypatch, capsys):
    from app.cli import check_central
    monkeypatch.setattr(settings, 'central_api_key', '')
    monkeypatch.setattr(check_central.httpx, 'Client', lambda **k: pytest.fail('unexpected network'))
    assert check_central.main() == 1
    assert 'DECKOUT_API_KEY' in capsys.readouterr().out


def test_preflight_uses_get_only(monkeypatch):
    import httpx
    from app.cli import check_central
    from app.central.client import REQUIRED_CAPABILITIES
    monkeypatch.setattr(settings, 'central_api_key', 'test-key')
    monkeypatch.setattr(settings, 'ingress_secret', 'test-secret')
    monkeypatch.setattr(settings, 'parent_channel_id', 123)
    monkeypatch.setattr(settings, 'skip_capability_check', False)
    monkeypatch.setattr(settings, 'allow_unauthenticated_local', False)
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('capabilities'):
            return httpx.Response(200, json=REQUIRED_CAPABILITIES)
        return httpx.Response(200, json={'status': 'ok'})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(check_central.httpx, 'Client', lambda **kwargs: client)
    assert check_central.main() == 0
    assert [r.method for r in requests] == ['GET', 'GET']
    assert requests[1].headers['X-API-Key'] == 'test-key'
