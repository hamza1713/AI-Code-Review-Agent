"""Freeze approved public PR inputs without exposing labels to a predictor."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


def fetch(url, accept="application/vnd.github+json"):
    request = Request(
        url, headers={"Accept": accept, "User-Agent": "code-review-corpus-freezer"}
    )
    with urlopen(request, timeout=30) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Corpus input exceeds size limit")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="samples/evaluation/public-pr-v1")
    args = parser.parse_args()
    root = Path(args.output)
    if root.exists():
        raise ValueError("Refusing to overwrite a frozen corpus")
    cases = []
    artifacts = {}
    repo = "pallets/flask"
    api = f"https://api.github.com/repos/{repo}"
    for number, cohort in [
        (5344, "clean_candidate"),
        (5383, "clean_candidate"),
        (5270, "hard_negative_candidate"),
    ]:
        metadata = json.loads(fetch(f"{api}/pulls/{number}"))
        if not metadata["merged_at"]:
            raise ValueError("Only merged PRs are accepted")
        base, head = metadata["base"]["sha"], metadata["head"]["sha"]
        license_info = json.loads(fetch(f"{api}/license?ref={head}"))
        if license_info["license"]["spdx_id"] not in (
            "BSD-3-Clause",
            "MIT",
            "Apache-2.0",
        ):
            raise ValueError("Source license is not approved")
        license_text = base64.b64decode(license_info["content"])
        diff_url = f"{api}/compare/{base}...{head}"
        comparison = json.loads(fetch(diff_url))
        if len(comparison.get("files", [])) != metadata["changed_files"]:
            raise ValueError("Comparison file count does not match PR")
        diff = fetch(diff_url, "application/vnd.github.diff")
        case_id = f"flask-{number}"
        artifacts[f"{case_id}.diff"] = diff
        artifacts[f"{case_id}.LICENSE"] = license_text
        cases.append(
            {
                "id": case_id,
                "file": f"{case_id}.diff",
                "sha256": hashlib.sha256(diff).hexdigest(),
                "source_url": metadata["html_url"],
                "source_repository": repo,
                "base_sha": base,
                "head_sha": head,
                "source_diff_url": diff_url,
                "license": license_info["license"]["spdx_id"],
                "license_file": f"{case_id}.LICENSE",
                "cohort": cohort,
                "label_status": "UNREVIEWED",
            }
        )
    root.mkdir(parents=True)
    for name, data in artifacts.items():
        (root / name).write_bytes(data)
    (root / "inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus_version": "public-pr-v1",
                "status": "AWAITING_INDEPENDENT_LABELS",
                "cases": cases,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Frozen {len(cases)} PRs; independent labels required before scoring.")


if __name__ == "__main__":
    main()
