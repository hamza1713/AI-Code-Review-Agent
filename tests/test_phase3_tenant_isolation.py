"""
Phase 3 Enterprise Multi-Tenancy & Tenant Crossover Isolation Tests.
Verifies that:
1. WebhookJobQueue rigorously enforces tenant isolation on enqueue, get_job, list_jobs, and cancel_job.
2. Cross-tenant access is strictly denied (returns None or False).
3. tenant_scope and resolve_tenant_from_request work seamlessly with headers and repository paths.
4. SecretsVault maintains tenant-isolated credentials.
"""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_review_agent.tenancy import (
    TenantAccessError,
    get_current_tenant,
    normalize_tenant_id,
    resolve_tenant_from_request,
    tenant_for_token,
    tenant_scope,
)
from code_review_agent.vault import SecretsVault
from code_review_agent.webhook_queue import WebhookJobQueue


@pytest.fixture
def temp_queue(tmp_path: Path) -> WebhookJobQueue:
    db_file = tmp_path / "tenant_test_queue.db"
    return WebhookJobQueue(db_path=str(db_file))


class TestTenantContext:
    def test_normalize_tenant_id(self):
        assert normalize_tenant_id(None) == "default"
        assert normalize_tenant_id("") == "default"
        assert normalize_tenant_id("  Acme-Corp  ") == "acme-corp"
        assert normalize_tenant_id("org_123:team-a") == "org_123:team-a"
        # Disallow unsafe SQL/path characters
        assert normalize_tenant_id("tenant'; DROP TABLE--") == "default"

    def test_tenant_scope_isolation(self):
        assert get_current_tenant() == "default"

        with tenant_scope("tenant_alpha"):
            assert get_current_tenant() == "tenant_alpha"

            with tenant_scope("tenant_beta"):
                assert get_current_tenant() == "tenant_beta"

            # Restores to outer scope
            assert get_current_tenant() == "tenant_alpha"

        # Restores to default
        assert get_current_tenant() == "default"

    def test_resolve_tenant_from_request(self):
        # Header resolution (case insensitive)
        assert (
            resolve_tenant_from_request({"X-Tenant-ID": "corp_finance"})
            == "corp_finance"
        )
        assert (
            resolve_tenant_from_request({"x-tenant-id": "engineering_platform"})
            == "engineering_platform"
        )

        # Repository owner resolution
        assert (
            resolve_tenant_from_request(
                repo_identifier="https://github.com/google/code-review-agent/pull/42"
            )
            == "google"
        )
        assert (
            resolve_tenant_from_request(
                repo_identifier="uber/rides-dispatch/pull/101"
            )
            == "uber"
        )

        # Fallback to default
        assert resolve_tenant_from_request({}) == "default"


class TestQueueTenantIsolation:
    def test_enqueue_and_list_isolation(self, temp_queue: WebhookJobQueue):
        # Enqueue jobs under two distinct enterprise tenants
        job_a1 = temp_queue.enqueue(
            pr_identifier="acme/repo1/pull/1",
            payload={"pr": 1},
            tenant_id="acme",
        )
        job_a2 = temp_queue.enqueue(
            pr_identifier="acme/repo1/pull/2",
            payload={"pr": 2},
            tenant_id="acme",
        )
        job_b1 = temp_queue.enqueue(
            pr_identifier="globex/service/pull/99",
            payload={"pr": 99},
            tenant_id="globex",
        )

        # List jobs for acme only
        acme_jobs = temp_queue.list_jobs(tenant_id="acme")
        acme_ids = {j["job_id"] for j in acme_jobs}
        assert job_a1 in acme_ids
        assert job_a2 in acme_ids
        assert job_b1 not in acme_ids

        # List jobs for globex only
        globex_jobs = temp_queue.list_jobs(tenant_id="globex")
        globex_ids = {j["job_id"] for j in globex_jobs}
        assert job_b1 in globex_ids
        assert job_a1 not in globex_ids
        assert job_a2 not in globex_ids

        # List all jobs with wildcard
        all_jobs = temp_queue.list_jobs(tenant_id="*")
        assert len(all_jobs) == 3

    def test_get_job_crossover_protection(self, temp_queue: WebhookJobQueue):
        job_id = temp_queue.enqueue(
            pr_identifier="secret-inc/core/pull/7",
            payload={"secret_code": "classified"},
            tenant_id="secret-inc",
        )

        # Authorized tenant can read
        authorized_job = temp_queue.get_job(job_id, tenant_id="secret-inc")
        assert authorized_job is not None
        assert authorized_job["job_id"] == job_id
        assert authorized_job["tenant_id"] == "secret-inc"

        # Unauthorized crossover tenant is denied (returns None, like 404)
        crossover_job = temp_queue.get_job(job_id, tenant_id="competitor-corp")
        assert crossover_job is None

    def test_cancel_job_crossover_protection(self, temp_queue: WebhookJobQueue):
        job_id = temp_queue.enqueue(
            pr_identifier="org1/app/pull/5",
            payload={"data": "test"},
            tenant_id="org1",
        )

        # Attacker / other tenant cannot cancel org1's job
        cancelled_by_other = temp_queue.cancel_job(
            job_id, reason="Malicious cancellation", tenant_id="org2"
        )
        assert cancelled_by_other is False

        # Job is still queued
        job = temp_queue.get_job(job_id, tenant_id="org1")
        assert job["status"] == "QUEUED"

        # Legitimate tenant cancels
        cancelled_by_owner = temp_queue.cancel_job(
            job_id, reason="User cancelled", tenant_id="org1"
        )
        assert cancelled_by_owner is True

        job = temp_queue.get_job(job_id, tenant_id="org1")
        assert job["status"] == "CANCELLED"


class TestSecretsVaultTenancy:
    def test_vault_tenant_isolated_secrets(self, monkeypatch):
        # Test tenant-specific env var override
        monkeypatch.setenv("TENANT_CORP_A_GITHUB_TOKEN", "token-for-corp-a")
        monkeypatch.setenv("TENANT_CORP_B_GITHUB_TOKEN", "token-for-corp-b")
        monkeypatch.setenv("GITHUB_TOKEN", "default-fallback-token")

        vault = SecretsVault()
        token_a = vault.get_tenant_git_token(tenant_id="corp-a")
        token_b = vault.get_tenant_git_token(tenant_id="corp-b")
        token_unknown = vault.get_tenant_git_token(tenant_id="corp-c")

        assert token_a == "token-for-corp-a"
        assert token_b == "token-for-corp-b"
        assert token_unknown == "default-fallback-token"


# --- Tenant credential binding (production-gaps day 1) -----------------------

OPERATOR_TOKEN = "o" * 40
ACME_TOKEN = "acme-tenant-token-" + "x" * 24


def _bind_tenants(monkeypatch):
    monkeypatch.setenv("REVIEW_MULTI_TENANT", "true")
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "true")
    monkeypatch.setenv("REVIEW_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv(
        "REVIEW_TENANT_TOKENS",
        "acme:" + hashlib.sha256(ACME_TOKEN.encode()).hexdigest(),
    )


def test_tenant_token_binding(monkeypatch):
    _bind_tenants(monkeypatch)
    assert tenant_for_token(ACME_TOKEN) == "acme"
    assert tenant_for_token("unknown-token") is None


def test_bound_tenant_rejects_spoofed_header():
    with pytest.raises(TenantAccessError):
        resolve_tenant_from_request({"X-Tenant-ID": "globex"}, bound_tenant="acme")
    assert resolve_tenant_from_request({"X-Tenant-ID": "acme"}, bound_tenant="acme") == "acme"
    assert resolve_tenant_from_request({}, bound_tenant="acme") == "acme"


def test_webhook_tenant_ignores_header():
    from code_review_agent.tenancy import resolve_tenant_from_request as resolve

    assert resolve(None, repo_identifier="github.com/acme/api") == "acme"


def test_gateway_tenant_token_spoof_and_scope(monkeypatch, tmp_path):
    _bind_tenants(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    from code_review_agent.webhook_server import app

    acme = {"Authorization": f"Bearer {ACME_TOKEN}"}
    with TestClient(app) as client:
        assert client.get("/jobs", headers=acme).status_code == 200
        spoofed = client.get("/jobs", headers={**acme, "X-Tenant-ID": "globex"})
        assert spoofed.status_code == 403
        # Tenant tokens cannot reach operator-only endpoints.
        assert client.get("/jobs/metrics", headers=acme).status_code == 403
        assert client.get("/api/calibration/metrics", headers=acme).status_code == 403
        session = client.get("/api/session", headers=acme).json()
        assert session["role"] == "tenant" and session["tenant"] == "acme"
        # The operator token keeps full access.
        operator = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        assert client.get("/jobs/metrics", headers=operator).status_code == 200
        assert client.get("/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
