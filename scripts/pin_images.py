"""Resolve official Docker base images to immutable manifest digests."""

import re
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
pins = {}
with httpx.Client(timeout=30) as client:
    for name, tag in [
        ("node", "22-alpine"),
        ("python", "3.12-slim"),
        ("caddy", "2-alpine"),
    ]:
        token_response = client.get(
            "https://auth.docker.io/token",
            params={
                "service": "registry.docker.io",
                "scope": f"repository:library/{name}:pull",
            },
        )
        token_response.raise_for_status()
        token = token_response.json()["token"]
        response = client.get(
            f"https://registry-1.docker.io/v2/library/{name}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
            },
        )
        response.raise_for_status()
        pins[name] = f"{name}:{tag}@{response.headers['docker-content-digest']}"
dockerfile = root / "Dockerfile"
text = dockerfile.read_text(encoding="utf-8")
for name in ("node", "python"):
    text = re.sub(rf"FROM {name}:[^\s]+", f"FROM {pins[name]}", text)
dockerfile.write_text(text, encoding="utf-8")
compose = root / "docker-compose.yml"
text = re.sub(
    r"image: caddy:[^\s]+",
    f"image: {pins['caddy']}",
    compose.read_text(encoding="utf-8"),
)
compose.write_text(text, encoding="utf-8")
print("\n".join(pins.values()))
