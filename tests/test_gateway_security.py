"""Security contracts at real API/middleware boundaries; no live provider calls."""
import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from code_review_agent.webhook_server import app
from code_review_agent.gateway_security import GatewaySecurityMiddleware, MAX_REQUEST_BYTES
from code_review_agent.review_service import rate_limiter
from code_review_agent.review_executor import ReviewDeadlineError
from code_review_agent.sandbox.test_runner import SandboxTestRunner, safe_diff_path

TOKEN = 'test-operator-token-with-32-characters-minimum'

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('REVIEW_API_TOKEN', TOKEN)
    monkeypatch.setenv('REVIEW_REQUIRE_AUTH', 'true')
    rate_limiter.reset()
    return TestClient(app, headers={'Authorization': f'Bearer {TOKEN}'})

@pytest.mark.parametrize('path', ['/api/session', '/jobs', '/jobs/private/result', '/api/webhook/config', '/api/cache/stats'])
def test_private_routes_require_auth(client, path):
    response = client.get(path, headers={'Authorization': 'Bearer wrong'})
    assert response.status_code == 401

def test_missing_api_config_fails_closed(monkeypatch):
    monkeypatch.setenv('REVIEW_REQUIRE_AUTH', 'true')
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    assert TestClient(app).get('/api/session').status_code == 503
    assert TestClient(app).get('/').status_code == 200

def test_production_environment_enforces_auth_by_default(monkeypatch):
    """Verify APP_ENV=production enforces operator auth even when REVIEW_REQUIRE_AUTH is unset."""
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.delenv('REVIEW_REQUIRE_AUTH', raising=False)
    monkeypatch.setenv('REVIEW_API_TOKEN', TOKEN)

    # Missing/wrong token returns 401
    assert TestClient(app).get('/jobs').status_code == 401
    # Valid token returns 200
    assert TestClient(app, headers={'Authorization': f'Bearer {TOKEN}'}).get('/jobs').status_code == 200

@pytest.mark.parametrize('payload', [
    {'command': '/review', 'repo_root': '/etc'},
    {'command': '/help', 'auto_post': True},
    {'command': '/help', 'pr_url': 'local:///private/repo'},
])
def test_public_api_cannot_access_host_or_post(client, monkeypatch, payload):
    monkeypatch.delenv('REVIEW_ALLOW_API_POSTING', raising=False)
    with patch('code_review_agent.webhook_server.run_job_async', new_callable=AsyncMock) as runner:
        assert client.post('/api/bot/command', json=payload).status_code in (400, 403)
        runner.assert_not_called()

def test_simulator_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv('REVIEW_ENABLE_TEST_EVENTS', raising=False)
    assert client.post('/api/webhook/test-event').status_code == 403

@pytest.mark.parametrize('provider', ['github', 'gitlab', 'bitbucket'])
def test_webhooks_require_configured_secrets(client, monkeypatch, provider):
    for name in ['GITHUB_WEBHOOK_SECRET', 'GITLAB_WEBHOOK_SECRET', 'GITLAB_TOKEN', 'BITBUCKET_WEBHOOK_SECRET']:
        monkeypatch.setenv(name, '')
    assert client.post(f'/webhook/{provider}', json={}).status_code == 401

def test_github_valid_signature_is_accepted(client, monkeypatch):
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'test-webhook-secret')
    body = b'{"action":"ignored"}'
    signature = 'sha256=' + hmac.new(b'test-webhook-secret', body, hashlib.sha256).hexdigest()
    assert client.post('/webhook/github', content=body, headers={'X-Hub-Signature-256': signature}).status_code == 200

def test_large_body_rejected_before_worker(client):
    with patch('code_review_agent.webhook_server.run_job_async', new_callable=AsyncMock) as runner:
        result = client.post('/api/review', content=b'x' * (MAX_REQUEST_BYTES + 1))
        assert result.status_code == 413
        runner.assert_not_called()

def test_chunked_body_cannot_bypass_limit():
    called = False
    sent = []
    async def downstream(scope, receive, send):
        nonlocal called
        called = True
    events = iter([{'type': 'http.request', 'body': b'x' * (MAX_REQUEST_BYTES // 2 + 1), 'more_body': True}] * 2)
    async def receive(): return next(events)
    async def send(event): sent.append(event)
    asyncio.run(GatewaySecurityMiddleware(downstream)({'type': 'http', 'method': 'POST',
        'path': '/webhook/github', 'headers': []}, receive, send))
    assert sent[0]['status'] == 413
    assert not called

def test_slow_body_deadline():
    sent = []
    async def receive(): await asyncio.sleep(1)
    async def send(event): sent.append(event)
    async def downstream(*args): pytest.fail('Slow request reached application')
    with patch('code_review_agent.gateway_security.BODY_TIMEOUT_SECONDS', 0.01):
        asyncio.run(GatewaySecurityMiddleware(downstream)({'type': 'http', 'method': 'POST',
            'path': '/webhook/github', 'headers': []}, receive, send))
    assert sent[0]['status'] == 408

def test_rate_limit_consumed_once_and_forwarding_headers_ignored(client):
    # Queue insertion is mocked; middleware and route quota accounting remain real.
    with patch('code_review_agent.webhook_server.queue.enqueue', return_value='job_test') as enqueue:
        for index in range(30):
            response = client.post('/api/review', json={'raw_diff': '+x'},
                headers={'Prefer': 'respond-async', 'X-Forwarded-For': f'10.0.0.{index}'})
            assert response.status_code == 202
        assert enqueue.call_count == 30
        response = client.post('/api/review', json={'raw_diff': '+x'},
            headers={'Prefer': 'respond-async', 'X-Forwarded-For': '192.0.2.1'})
        assert response.status_code == 429
        assert response.headers['Retry-After']

def test_timeout_response_and_stream_are_honest(client):
    with patch('code_review_agent.webhook_server.run_job_async', new_callable=AsyncMock, side_effect=ReviewDeadlineError()):
        assert client.post('/api/review', json={'raw_diff': '+x'}).status_code == 504
        response = client.post('/api/review/stream', json={'raw_diff': '+x'})
        assert 'event: error' in response.text
        assert 'progress' not in response.text
        assert 'SECURITY_SCAN' not in response.text

@pytest.mark.parametrize('name', ['../outside.py', '/absolute.py', 'C:\\outside.py', '..\\outside.py', '\\\\host\\share\\x.py', 'safe/../../bad.py', 'file.py:stream', 'NUL', 'CON.py'])
def test_diff_paths_stay_inside_workspace(tmp_path, name):
    with pytest.raises(ValueError): safe_diff_path(tmp_path / 'workspace', name)
    assert not list(tmp_path.iterdir())

def test_disabled_sandbox_never_launches_host_python(monkeypatch):
    monkeypatch.delenv('REVIEW_SANDBOX_IMAGE', raising=False)
    with patch('subprocess.Popen') as execute:
        result = SandboxTestRunner.run_tests('import os\nos._exit(1)')
        assert not result.executed
        assert result.evidence_badge == 'UNVERIFIED'
        execute.assert_not_called()

def test_unknown_sandbox_image_fails_closed(monkeypatch):
    monkeypatch.setenv('REVIEW_SANDBOX_IMAGE', 'python:latest')
    with patch('subprocess.Popen') as execute:
        assert SandboxTestRunner.run_tests('assert True').status == 'ERROR'
        execute.assert_not_called()
