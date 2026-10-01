"""One-to-one matching by defect identity and full path within a stated tolerance."""


def score_findings(
    detected: list[dict], expected: list[dict], tolerance: int = 0
) -> dict:
    edges = {}
    for i, gold in enumerate(expected):
        if not gold.get("file_path") or not gold.get("line_number"):
            raise ValueError("Gold labels require full file path and line number")
        if not gold.get("rule_ids") and not gold.get("cwe"):
            raise ValueError("Gold labels require defect identity (rule_ids or CWE)")
        edges[i] = [
            j
            for j, prediction in enumerate(detected)
            if prediction.get("file_path") == gold["file_path"]
            and abs(prediction.get("line_number", 0) - gold["line_number"]) <= tolerance
            and (
                prediction.get("rule_id") in gold.get("rule_ids", [])
                or bool(gold.get("cwe") and prediction.get("cwe") == gold["cwe"])
            )
        ]
    # Maximum bipartite matching avoids label-order-dependent greedy scores.
    matches: dict[int, int] = {}

    def assign(i, visited):
        for j in edges[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in matches or assign(matches[j], visited):
                matches[j] = i
                return True
        return False

    for i in edges:
        assign(i, set())
    return {
        "tp": len(matches),
        "fp": len(detected) - len(matches),
        "fn": len(expected) - len(matches),
        "unmatched_predictions": [j for j in range(len(detected)) if j not in matches],
    }


def predict_verdict(detected: list[dict]) -> str:
    """Prediction uses output only; labels are supplied exclusively to scoring."""
    return "REQUEST CHANGES" if detected else "APPROVE"
