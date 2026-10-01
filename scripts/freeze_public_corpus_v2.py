"""Freeze 120 permissively licensed merged PRs with immutable full-head source."""

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from code_review_agent.diff_parser import DiffParser
from code_review_agent.path_validation import safe_diff_path
from code_review_agent.source_snapshot import build_snapshot

REPOSITORIES = {
    "pallets/flask": "BSD-3-Clause",
    "encode/httpx": "BSD-3-Clause",
    "psf/requests": "Apache-2.0",
}


def fetch(url: str, limit: int = 2 * 1024 * 1024) -> bytes:
    for attempt in range(3):
        try:
            with urlopen(
                Request(
                    url,
                    headers={
                        "User-Agent": "code-review-corpus-freezer",
                        "Accept": "application/vnd.github+json",
                    },
                ),
                timeout=30,
            ) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Source exceeds acquisition limit")
            return data
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("Acquisition failed")


def license_at(repo: str, commit: str, spdx: str) -> tuple[str, bytes]:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.rst", "LICENSE.txt"):
        try:
            data = fetch(f"https://raw.githubusercontent.com/{repo}/{commit}/{name}")
        except HTTPError as error:
            if error.code == 404:
                continue
            raise
        text = data.decode("utf-8").lower()
        valid = (
            spdx == "Apache-2.0"
            and "apache license" in text
            and "version 2.0" in text
            and "terms and conditions" in text
        ) or (
            spdx == "BSD-3-Clause"
            and "redistribution" in text
            and "source" in text
            and "binary" in text
            and "neither" in text
            and "disclaimer" in text
        )
        if not valid:
            raise ValueError(
                "Captured revision license does not match approved SPDX template"
            )
        return name, data
    raise ValueError("Captured revision has no approved license")


def freeze_case(root: Path, repo: str, item: dict, spdx: str) -> dict:
    number, base, head = item["number"], item["base"]["sha"], item["head"]["sha"]
    case_id = repo.split("/")[1] + "-" + str(number)
    cached = root / "cases" / case_id / "case.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    diff_url = f"https://github.com/{repo}/compare/{base}...{head}.diff"
    raw_diff = fetch(diff_url)
    diff = raw_diff.decode("utf-8")
    parsed = DiffParser.parse_diff(diff)
    if not parsed.files or not any(
        f.target_file.endswith(".py") and not f.is_deleted_file for f in parsed.files
    ):
        raise ValueError("No changed Python source")
    source_repo = (item["head"].get("repo") or {}).get("full_name") or repo

    def source(path):
        encoded = quote(path, safe="/")
        try:
            return fetch(
                f"https://raw.githubusercontent.com/{source_repo}/{head}/{encoded}"
            )
        except HTTPError as error:
            if error.code != 404 or source_repo == repo:
                raise
            return fetch(f"https://raw.githubusercontent.com/{repo}/{head}/{encoded}")

    snapshot = build_snapshot(diff, head, f"github:github.com/{repo}", fetch=source)
    if any(c.status not in {"AVAILABLE", "DELETED"} for c in snapshot.coverage):
        raise ValueError(
            "Full head acquisition incomplete: "
            + ",".join(c.status for c in snapshot.coverage)
        )
    license_name, license_data = license_at(repo, head, spdx)
    folder = root / "cases" / case_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "input.diff").write_bytes(raw_diff)
    (folder / "LICENSE").write_bytes(license_data)
    files = []
    for artifact in snapshot.files:
        target = safe_diff_path(folder / "head", artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.raw)
        files.append(
            {
                "path": artifact.path,
                "file": target.relative_to(root).as_posix(),
                "sha256": artifact.sha256,
            }
        )
    case = {
        "id": case_id,
        "file": (folder / "input.diff").relative_to(root).as_posix(),
        "sha256": hashlib.sha256(raw_diff).hexdigest(),
        "source_repository": repo,
        "base_sha": base,
        "head_sha": head,
        "source_diff_url": diff_url,
        "source_url": item["html_url"],
        "merged_at": item["merged_at"],
        "license": spdx,
        "license_file": (folder / "LICENSE").relative_to(root).as_posix(),
        "license_sha256": hashlib.sha256(license_data).hexdigest(),
        "source_license_path": license_name,
        "head_files": files,
        "input_snapshot_hash": snapshot.snapshot_hash,
        "label_status": "UNREVIEWED",
    }
    cached.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
    return case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("samples/evaluation/public-pr-v2")
    )
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite a frozen corpus")
    args.output = args.output.resolve()
    root = args.output.with_name(args.output.name + ".building")
    root.mkdir(parents=True, exist_ok=True)
    exclusions = []
    exclusion_file = root / "exclusions.json"
    if exclusion_file.exists():
        exclusions = json.loads(exclusion_file.read_text(encoding="utf-8"))
    selected = []
    for repo, spdx in REPOSITORIES.items():
        repo_meta = json.loads(fetch(f"https://api.github.com/repos/{repo}"))
        if (repo_meta.get("license") or {}).get("spdx_id") != spdx:
            raise ValueError("Repository license approval changed")
        eligible = []
        for page in range(1, 11):
            metadata = root / (repo.replace("/", "-") + f"-page-{page}.json")
            if metadata.exists():
                candidates = json.loads(metadata.read_text(encoding="utf-8"))
            else:
                # Selection metadata is kept separately from predictor inputs.
                data = fetch(
                    f"https://api.github.com/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
                    16 * 1024 * 1024,
                )
                raw = json.loads(data)
                candidates = [
                    {
                        key: item[key]
                        for key in (
                            "number",
                            "base",
                            "head",
                            "html_url",
                            "merged_at",
                            "updated_at",
                        )
                    }
                    for item in raw
                ]
                # No titles or discussions are retained, even in staging metadata.
                metadata.write_text(json.dumps(candidates), encoding="utf-8")
            merged = sorted(
                (c for c in candidates if c["merged_at"]),
                key=lambda c: (c["merged_at"], c["number"]),
                reverse=True,
            )

            def capture(item, repo=repo, spdx=spdx):
                try:
                    return freeze_case(root, repo, item, spdx), None
                except (OSError, RuntimeError, ValueError) as error:
                    return None, {
                        "repository": repo,
                        "pr": item["number"],
                        "reason": str(error),
                    }

            with ThreadPoolExecutor(max_workers=4) as pool:
                for case, excluded in pool.map(capture, merged):
                    if case:
                        eligible.append(case)
                    else:
                        exclusions.append(excluded)
                    print(f"{repo}: {len(eligible)} eligible", flush=True)
                    exclusion_file.write_text(
                        json.dumps(exclusions, indent=2), encoding="utf-8"
                    )
            eligible = sorted(
                {c["id"]: c for c in eligible}.values(),
                key=lambda c: (c["merged_at"], c["id"]),
                reverse=True,
            )
            # A merge cannot be later than its update; this closes the selection window.
            if len(eligible) >= 40 and (
                not candidates
                or candidates[-1]["updated_at"] <= eligible[39]["merged_at"]
            ):
                break
            if not candidates:
                break
        if len(eligible) < 40:
            raise RuntimeError(
                f"Only {len(eligible)} eligible PRs in {repo}; staging retained"
            )
        selected.extend(eligible[:40])
    ordered = sorted(
        selected,
        key=lambda c: hashlib.sha256(("split-v1:" + c["id"]).encode()).hexdigest(),
    )
    development = {c["id"] for c in ordered[:20]}
    for case in selected:
        case["split"] = "development" if case["id"] in development else "held_out"
    manifest = {
        "schema_version": 2,
        "corpus_version": "public-pr-v2",
        "status": "AWAITING_INDEPENDENT_LABELS",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "selection": "40 per repository; merged_at descending within closed update window",
        "split_algorithm": "SHA256('split-v1:' + case_id), first 20 development; remaining 100 held_out",
        "cases": selected,
    }
    (root / "inputs.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (root / "manifest.sha256").write_text(
        hashlib.sha256((root / "inputs.json").read_bytes()).hexdigest() + "\n",
        encoding="utf-8",
    )
    # Predictor loader verifies all retained hashes before the directory becomes frozen.
    from code_review_agent.eval.corpus import load_inputs

    load_inputs(root)
    root.rename(args.output)
    print(
        "Frozen 120 PRs: 20 development, 100 held out; independent labels required",
        flush=True,
    )


if __name__ == "__main__":
    main()
