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
