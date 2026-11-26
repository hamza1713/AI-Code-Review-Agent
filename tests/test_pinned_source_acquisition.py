"""Exact commit, tree mode and bytes must agree before source is trusted."""

import base64
import hashlib

import httpx
import pytest

from code_review_agent.github_client import GitHubClient

HEAD = "a" * 40
TREE = "b" * 40
SOURCE = b"def run(value):\n    return value\n"


def install_source_api(monkeypatch, corruption=None):
    blob = hashlib.sha1(
        b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE
    ).hexdigest()
    requests = []

    def respond(request):
        requests.append(request)
        prefix = "/repos/fork-owner/reviewed"
        if request.url.path == prefix + "/git/commits/" + HEAD:
            data = {"sha": HEAD, "tree": {"sha": TREE}}
        elif request.url.path == prefix + "/git/trees/" + TREE:
            data = {
                "sha": TREE,
                "truncated": False,
                "tree": [
                    {
                        "path": "api/utils.py",
                        "type": "blob",
                        "mode": "100644",
                        "sha": blob,
                        "size": len(SOURCE),
                    }
                ],
            }
            if corruption == "symlink":
                data["tree"][0]["mode"] = "120000"
            elif corruption == "tree":
                data["sha"] = "c" * 40
            elif corruption == "truncated":
                data["truncated"] = True
            elif corruption == "oversize":
                data["tree"][0]["size"] = 2 * 1024 * 1024 + 1
        elif request.url.path == prefix + "/contents/api/utils.py":
            assert request.url.params["ref"] == HEAD
            data = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(SOURCE).decode(),
                "sha": blob,
                "size": len(SOURCE),
            }
            if corruption == "blob":
                data["content"] = base64.b64encode(
                    SOURCE.replace(b"value", b"other")
                ).decode()
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=data)

    original = httpx.Client
    monkeypatch.setattr(
        "code_review_agent.github_client.httpx.Client",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    return requests


def test_fork_source_is_pinned_and_tree_is_cached(monkeypatch):
    requests = install_source_api(monkeypatch)
    client = GitHubClient(token="test-only")
    assert (
        client.fetch_source_file("fork-owner", "reviewed", "api/utils.py", HEAD)
        == SOURCE
    )
    assert (
        client.fetch_source_file("fork-owner", "reviewed", "api/utils.py", HEAD)
        == SOURCE
    )
    assert len(requests) == 4


@pytest.mark.parametrize(
    "corruption", ["symlink", "tree", "truncated", "oversize", "blob"]
)
def test_unverified_source_is_rejected(monkeypatch, corruption):
    requests = install_source_api(monkeypatch, corruption)
    with pytest.raises((ValueError, OverflowError)):
        GitHubClient(token="test-only").fetch_source_file(
            "fork-owner", "reviewed", "api/utils.py", HEAD
        )
    assert len(requests) == (3 if corruption == "blob" else 2)


def test_mutable_source_ref_rejected_without_network(monkeypatch):
    requests = install_source_api(monkeypatch)
    with pytest.raises(ValueError, match="immutable"):
        GitHubClient(token="test-only").fetch_source_file(
            "fork-owner", "reviewed", "api/utils.py", "main"
        )
    assert not requests


def test_stale_token_retries_without_authorization_header(monkeypatch):
    blob = hashlib.sha1(
        b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE
    ).hexdigest()
    seen_auth_headers = []

    def respond(request):
        auth = request.headers.get("authorization")
        seen_auth_headers.append((request.url.path, auth))
        if auth == "Bearer expired-token":
            return httpx.Response(401, json={"message": "Bad credentials"})

        prefix = "/repos/fork-owner/reviewed"
        if request.url.path == f"{prefix}/pulls/7":
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text="diff --git a/api/utils.py b/api/utils.py\n+ok\n")
            return httpx.Response(
                200,
                json={
                    "title": "Public fix",
                    "body": "",
                    "user": {"login": "octocat"},
                    "base": {"sha": "0" * 40, "ref": "main"},
                    "head": {"sha": HEAD, "ref": "fix", "repo": {"full_name": "fork-owner/reviewed"}},
                    "state": "open",
                    "html_url": "https://github.com/fork-owner/reviewed/pull/7",
                },
            )
        if request.url.path == f"{prefix}/git/commits/{HEAD}":
            return httpx.Response(200, json={"sha": HEAD, "tree": {"sha": TREE}})
        if request.url.path == f"{prefix}/git/trees/{TREE}":
            return httpx.Response(
                200,
                json={
                    "sha": TREE,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "api/utils.py",
                            "type": "blob",
                            "mode": "100644",
                            "sha": blob,
                            "size": len(SOURCE),
                        }
                    ],
                },
            )
        if request.url.path == f"{prefix}/contents/api/utils.py":
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(SOURCE).decode(),
                    "sha": blob,
                    "size": len(SOURCE),
                },
            )
        raise AssertionError(str(request.url))

    original = httpx.Client
    monkeypatch.setattr(
        "code_review_agent.github_client.httpx.Client",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )

    client = GitHubClient(token="expired-token")
    meta = client.fetch_pull_request_metadata("fork-owner", "reviewed", 7)
    diff = client.fetch_pull_request_diff("fork-owner", "reviewed", 7)
    raw = client.fetch_source_file("fork-owner", "reviewed", "api/utils.py", HEAD)

    assert meta["head_sha"] == HEAD
    assert "+ok" in diff
    assert raw == SOURCE
    # Ensure failed attempts included Bearer and retried unauthenticated without Authorization
    assert seen_auth_headers[0][1] == "Bearer expired-token"
    assert seen_auth_headers[1][1] is None
    assert any(auth is None and path.endswith("/contents/api/utils.py") for path, auth in seen_auth_headers)


def test_unauthenticated_retry_rejected_fails_closed(monkeypatch):
    """A private repo rejects both the stale token and the anonymous retry."""
    seen_auth_headers = []

    def respond(request):
        seen_auth_headers.append(request.headers.get("authorization"))
        return httpx.Response(401, json={"message": "Bad credentials"})

    original = httpx.Client
    monkeypatch.setattr(
        "code_review_agent.github_client.httpx.Client",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )

    client = GitHubClient(token="expired-token")
    with pytest.raises(RuntimeError, match="401"):
        client.fetch_source_file("fork-owner", "reviewed", "api/utils.py", HEAD)
    assert seen_auth_headers == ["Bearer expired-token", None]
