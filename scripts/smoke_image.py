"""Start and authenticate against the exact built production image; no model calls."""

import argparse
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def smoke(image: str) -> dict:
    info = json.loads(docker("image", "inspect", image))[0]
    identity = info["Id"]
    commit = (info["Config"].get("Labels") or {}).get(
        "org.opencontainers.image.revision"
    )
    assert commit, "Built image must identify its source commit"
    assert info["Config"]["User"] == "10001:10001", (
        "Image must run as the unprivileged service user"
    )
    name = "review-smoke-" + secrets.token_hex(6)
    token = secrets.token_urlsafe(36)
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,size=2g",
        "--tmpfs",
        "/data:rw,nosuid,uid=10001,gid=10001",
        "-p",
        "127.0.0.1::8000",
        "-e",
        "APP_ENV=production",
        "-e",
        "REVIEW_API_TOKEN=" + token,
        "-e",
        "REVIEW_REQUIRE_AUTH=true",
        "-e",
        "REVIEW_ENABLE_POSTING=false",
        "-e",
        "REVIEW_ALLOWED_REPOSITORIES=github:github.com/example/smoke",
        identity,
    )
    try:
        assert json.loads(docker("inspect", container))[0]["Image"] == identity
        port = docker("port", container, "8000/tcp").split(":")[-1]
        base = "http://127.0.0.1:" + port

        def request(path, authenticated=False):
            req = urllib.request.Request(
                base + path,
                headers={"Authorization": "Bearer " + token} if authenticated else {},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        deadline = time.monotonic() + 90
        while True:
            try:
                if request("/health/ready") == 200:
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Built image failed readiness")
            time.sleep(0.5)
        for path in ("/jobs", "/metrics", "/docs", "/openapi.json"):
            assert request(path) == 401, path + " must require authentication"
            assert request(path, True) == 200, path + " must work after authentication"
        assert request("/") == 200
        profile = json.loads(
            docker(
                "exec",
                container,
                "python",
                "-c",
                "import json; from code_review_agent.reviewer_profile import build_profile; print(json.dumps(build_profile().model_dump()))",
            )
        )
        return {
            "status": "PASS",
            "image_digest": identity,
            "commit": commit,
            "reviewer_profile": profile,
            "image_digest_kind": "local image content ID",
            "non_root": True,
            "read_only": True,
            "authentication": "PASS",
            "readiness": "PASS",
        }
    finally:
        docker("rm", "-f", container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = smoke(args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
