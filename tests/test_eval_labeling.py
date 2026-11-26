"""Human labeling workflow: merge, agreement, intervals and the operator API."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_review_agent.eval.adjudication import (
    ADJUDICATED,
    NEEDS_ADJUDICATION,
    agreement_report,
    cohens_kappa,
    merge_labels,
    resolve_disagreement,
    wilson_interval,
)
from code_review_agent.webhook_server import app

CORPUS = Path(__file__).resolve().parents[1] / "samples" / "evaluation" / "public-pr-v2"
FINDING = {"file_path": "src/flask/app.py", "line_number": 12, "rule_ids": ["SEC-001"]}


def _reviewer_file(name, cases):
    return {
        "reviewer": name,
        "corpus_manifest_sha256": "m",
        "corpus_version": "v",
        "schema_version": 2,
        "cases": cases,
    }


def _case(case_id, cohort, findings=(), status="LABELED"):
    return {
        "id": case_id,
        "sha256": "s",
        "input_snapshot_hash": "h",
        "status": status,
        "reviewers": [],
        "cohort": cohort,
        "rationale": "because",
        "findings": [
            {**f, "rule_ids": sorted(f["rule_ids"]), "cwe": None} for f in findings
        ],
    }


def test_wilson_interval_bounds():
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(8, 10)
    assert 0.44 < low < 0.5 and 0.94 < high < 0.97
    assert wilson_interval(10, 10)[1] == 1.0


def test_cohens_kappa():
    assert cohens_kappa([]) is None
    assert cohens_kappa([("clean", "clean"), ("defect", "defect")]) == 1.0
    assert cohens_kappa([("clean", "defect"), ("defect", "clean")]) < 0


def test_merge_agreement_and_disagreement():
    a = _reviewer_file("Ana", [_case("c1", "clean"), _case("c2", "defect", [FINDING])])
    b = _reviewer_file("Ben", [_case("c1", "clean"), _case("c2", "clean")])
    merged = merge_labels(a, b)
    by_id = {c["id"]: c for c in merged["cases"]}
    assert by_id["c1"]["status"] == ADJUDICATED
    assert by_id["c1"]["reviewers"] == ["Ana", "Ben"]
    assert by_id["c2"]["status"] == NEEDS_ADJUDICATION
    assert by_id["c2"]["disagreement"] is True

    with pytest.raises(ValueError):
        resolve_disagreement(by_id["c2"], "Ana", "defect", "x", [FINDING])
    resolved = resolve_disagreement(by_id["c2"], "Cara", "defect", "confirmed", [FINDING])
    assert resolved["status"] == ADJUDICATED and resolved["adjudicator"] == "Cara"

    # A later merge with unchanged labels keeps the third-party resolution.
    merged["cases"] = [resolved if c["id"] == "c2" else c for c in merged["cases"]]
    again = merge_labels(a, b, merged)
    assert {c["id"]: c for c in again["cases"]}["c2"]["adjudicator"] == "Cara"

    report = agreement_report(a, b)
    assert report["cases_compared"] == 2 and report["cohort_agreement"] == 0.5


def test_merge_refuses_same_person():
    a = _reviewer_file("Ana", [_case("c1", "clean")])
    with pytest.raises(ValueError):
        merge_labels(a, _reviewer_file("ana", [_case("c1", "clean")]))


@pytest.fixture
def corpus_copy(tmp_path, monkeypatch):
    target = tmp_path / "public-pr-v2"
    shutil.copytree(CORPUS, target)
    monkeypatch.setenv("REVIEW_EVAL_CORPUS", str(target))
    monkeypatch.setenv("REVIEW_EVAL_PREDICTIONS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    return target


def test_eval_api_blind_labeling_flow(corpus_copy):
    with TestClient(app) as client:
        listing = client.get("/api/eval/cases").json()
        assert listing["split"] == "development"
        case_id = listing["cases"][0]["id"]

        detail = client.get(f"/api/eval/cases/{case_id}?reviewer=b").json()
        assert "diff --git" in detail["diff"]
        assert detail["label"]["status"] == "UNREVIEWED"

        body = {"reviewer_name": "Ana", "cohort": "clean", "rationale": "docs only"}
        assert client.put(f"/api/eval/labels/a/{case_id}", json=body).status_code == 200
        # The same person cannot also be reviewer B.
        assert client.put(f"/api/eval/labels/b/{case_id}", json=body).status_code == 422
        # A defect label needs a finding.
        bad = {"reviewer_name": "Ben", "cohort": "defect", "rationale": "x"}
        assert client.put(f"/api/eval/labels/b/{case_id}", json=bad).status_code == 422
        ok = client.put(
            f"/api/eval/labels/b/{case_id}",
            json={"reviewer_name": "Ben", "cohort": "clean", "rationale": "agree"},
        )
        assert ok.status_code == 200

        merged = client.post("/api/eval/merge").json()
        assert merged["progress"]["adjudicated"] == 1
        score = client.get("/api/eval/score").json()
        assert score["ready"] is False and score["progress"]["adjudicated"] == 1

    saved = json.loads((corpus_copy / "labeling" / "development-reviewer-a.json").read_text())
    assert saved["reviewer"] == "Ana"


def test_eval_api_never_serves_held_out(corpus_copy):
    manifest = json.loads((corpus_copy / "inputs.json").read_text())
    held_out = next(c["id"] for c in manifest["cases"] if c["split"] == "held_out")
    with TestClient(app) as client:
        assert client.get(f"/api/eval/cases/{held_out}").status_code == 404
        ids = {c["id"] for c in client.get("/api/eval/cases").json()["cases"]}
    assert held_out not in ids
