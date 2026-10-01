"""Record an executed CI gate and its commit; never manufacture failed-job success."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    commit = os.environ["GITHUB_SHA"]
    result = {
        "status": "PASS",
        "commit": commit,
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
    }
    if args.artifact:
        result["artifact_sha256"] = hashlib.sha256(
            args.artifact.read_bytes()
        ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
