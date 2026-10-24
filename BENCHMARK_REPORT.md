# Deterministic scanner regression report

Scorer version: 2. Synthetic fixtures; not a product quality study.
Full path, exact target line, and rule ID or CWE define a match. One-to-one matching; every unmatched emitted hit is an FP.
Predicted verdict uses emitted output only. Gold labels never enter prediction.
TP/FP/FN count findings; TN counts clean cases, so these are not a PR confusion matrix.
Zero-denominator precision/recall are reported as 0, not evidence of perfect performance.

# 🎯 Model & Code Review Benchmark Results

| Overall Metric | Value |
| :--- | :--- |
| **Total Test Cases** | 14 |
| **True Positives (TP)** | 18 |
| **False Positives (FP)** | 0 |
| **False Negatives (FN)** | 0 |
| **Overall Precision** | 100.0% |
| **Overall Recall** | 100.0% |
| **Overall F1-Score** | 100.0% |
| **Verdict Accuracy** | 100.0% |
| **Execution Duration** | 15.036s |

### 📊 Breakdown by Domain Category

| Category | Cases | Precision | Recall | F1-Score | Verdict Accuracy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SECURITY** | 7 | 100.0% | 100.0% | 100.0% | 100.0% |
| **QUALITY** | 3 | 100.0% | 100.0% | 100.0% | 100.0% |
| **GOVERNANCE** | 2 | 100.0% | 100.0% | 100.0% | 100.0% |
| **ARCHITECTURE** | 1 | 100.0% | 100.0% | 100.0% | 100.0% |
| **COMPLEX** | 1 | 100.0% | 100.0% | 100.0% | 100.0% |

### 📋 Detailed Case Breakdown

| ID | Test Case | Category | Expected | Detected | TP | FP | FN | Verdict Match |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| SEC-001 | SQL Injection Detection | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-002 | Hardcoded Secret Detection | SECURITY | 2 | 2 | 2 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-003 | Command Injection Detection | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-004 | Insecure Deserialization Detection | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-005 | Plaintext Password Comparison | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| QUAL-001 | Clean Code Verification | QUALITY | 0 | 0 | 0 | 0 | 0 | ✅ APPROVE |
| QUAL-002 | N+1 ORM Query Loop | QUALITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| QUAL-003 | Swallowed Exception Anti-Pattern | QUALITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| GOV-001 | Debug Print Statements | GOVERNANCE | 2 | 2 | 2 | 0 | 0 | ✅ REQUEST CHANGES |
| GOV-002 | Wildcard Imports | GOVERNANCE | 2 | 2 | 2 | 0 | 0 | ✅ REQUEST CHANGES |
| ARCH-001 | Breaking API Signature | ARCHITECTURE | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| COMPLEX-001 | Combined Multi-Vulnerability PR | COMPLEX | 3 | 3 | 3 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-006 | Weak Cryptographic Hash (MD5) | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |
| SEC-007 | Path Traversal Arbitrary File Read | SECURITY | 1 | 1 | 1 | 0 | 0 | ✅ REQUEST CHANGES |

Reproduce: `python -m code_review_agent.benchmarks --publish`