import urllib.request
import json
from pathlib import Path

diff_text = Path("samples/synthetic_vulnerability_test.diff").read_text(encoding="utf-8")
payload = json.dumps({"raw_diff": diff_text}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:8000/api/review",
    data=payload,
    headers={"Content-Type": "application/json", "Prefer": "respond-sync"},
)

try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        print("HTTP Status:", resp.status)
        print("Verdict:", result.get("verdict"))
        print("Quality Score:", result.get("quality_score"))
        findings = result.get("reconciled_findings") or result.get("findings") or []
        print(f"Total Reconciled Findings: {len(findings)}")
        for f in findings:
            print(f"  - [{f.get('rule_id')}] ({f.get('severity')}): {f.get('description')}")
except urllib.error.HTTPError as e:
    print("HTTPError:", e.code, e.read().decode("utf-8"))
except Exception as e:
    print("Error:", type(e), e)
