# 🎯 Code Review Ground-Truth Benchmark Report

[![Benchmark F1](https://img.shields.io/badge/Benchmark_F1-88.4%25-brightgreen)](#) [![Verdict Accuracy](https://img.shields.io/badge/Verdict_Accuracy-100.0%25-success)](#) [![Deterministic SAST](https://img.shields.io/badge/Deterministic_Grounding-Compiler--Grade-blue)](#) [![Test Cases](https://img.shields.io/badge/Evaluated_Cases-14-orange)](#)

## Executive Summary
The AI Code Review Agent was evaluated against **14 curated ground-truth pull requests** spanning Security Vulnerabilities (OWASP Top 10, CWE-89, CWE-78, CWE-798, CWE-502), Architecture & Governance Violations (.code-review.yaml), and Clean PR Control Cases.

- **Overall F1 Score**: **`88.4%`** (Precision: `79.2%`, Recall: `100.0%`)
- **Merge Verdict Accuracy**: **`100.0%`** (Correct APPROVE vs REQUEST CHANGES decisions)
- **Benchmark Runtime**: `15.060s` across all 14 test cases

---

## 🥊 Positioning vs Qodo Merge

> ⚠️ **Not a head-to-head benchmark.** Our F1 of **`88.4%`** is measured on *this project's own 14-case ground-truth suite*. Qodo's reported **`60.1%`** was measured on a different, independent dataset. The two numbers are **not directly comparable** and one should not be subtracted from the other — treat our score as an internal quality signal, not proof of superiority. The rows below are genuine *architectural* differences.

| Dimension | Our Platform | Qodo Merge |
| :--- | :--- | :--- |
| **Pre-Scan Grounding** | Compiler-grade SAST + AST call-graph | LLM heuristics |
| **Empirical Test Verification** | Subprocess sandbox runs generated pytest | Generation only |
| **Air-Gapped Offline Support** | Native local Git adapter | Cloud-only |

---

## 🧮 Mathematical Evaluation Metrics

Metrics are computed using standard information retrieval and classification formulations:

$$\text{Precision} = \frac{TP}{TP + FP} = \frac{19}{24} = 0.7917$$

$$\text{Recall} = \frac{TP}{TP + FN} = \frac{19}{19} = 1.0000$$

$$F_1 = 2 \cdot \frac{\text{Precision} \cdot \text{Recall}}{\text{Precision} + \text{Recall}} = 0.8837$$

### 2×2 Confusion Matrix

| | Actually Vulnerable / Defective | Actually Clean |
| :--- | :---: | :---: |
| **Predicted Flagged** | **True Positive (TP)**: `19` | **False Positive (FP)**: `5` |
| **Predicted Clean** | **False Negative (FN)**: `0` | **True Negative (TN)**: `1` |

---

## 📊 Domain Category Breakdown

| Category | Test Cases | Precision | Recall | F1 Score | Verdict Accuracy |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **SECURITY** | 7 | 69.2% | 100.0% | **81.8%** | 100.0% |
| **QUALITY** | 3 | 100.0% | 100.0% | **100.0%** | 100.0% |
| **GOVERNANCE** | 2 | 100.0% | 100.0% | **100.0%** | 100.0% |
| **ARCHITECTURE** | 1 | 100.0% | 100.0% | **100.0%** | 100.0% |
| **COMPLEX** | 1 | 75.0% | 100.0% | **85.7%** | 100.0% |

---

## 📋 Comprehensive Case Audit

| ID | Test Case Name | Category | Expected | Detected | TP | FP | FN | Verdict Match |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `SEC-001` | SQL Injection Detection | `SECURITY` | 1 | 2 | 1 | 1 | 0 | ✅ PASS |
| `SEC-002` | Hardcoded Secret Detection | `SECURITY` | 2 | 4 | 2 | 2 | 0 | ✅ PASS |
| `SEC-003` | Command Injection Detection | `SECURITY` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |
| `SEC-004` | Insecure Deserialization Detection | `SECURITY` | 1 | 2 | 1 | 1 | 0 | ✅ PASS |
| `SEC-005` | Plaintext Password Comparison | `SECURITY` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |
| `QUAL-001` | Clean Code Verification | `QUALITY` | 0 | 0 | 0 | 0 | 0 | ✅ PASS |
| `QUAL-002` | N+1 ORM Query Loop | `QUALITY` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |
| `QUAL-003` | Swallowed Exception Anti-Pattern | `QUALITY` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |
| `GOV-001` | Debug Print Statements | `GOVERNANCE` | 2 | 2 | 2 | 0 | 0 | ✅ PASS |
| `GOV-002` | Wildcard Imports | `GOVERNANCE` | 2 | 2 | 2 | 0 | 0 | ✅ PASS |
| `ARCH-001` | Breaking API Signature | `ARCHITECTURE` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |
| `COMPLEX-001` | Combined Multi-Vulnerability PR | `COMPLEX` | 3 | 4 | 3 | 1 | 0 | ✅ PASS |
| `SEC-006` | Weak Cryptographic Hash (MD5) | `SECURITY` | 2 | 2 | 2 | 0 | 0 | ✅ PASS |
| `SEC-007` | Path Traversal Arbitrary File Read | `SECURITY` | 1 | 1 | 1 | 0 | 0 | ✅ PASS |

---

## 🔁 Reproducibility Command
To reproduce this benchmark report from source code:
```bash
python -m code_review_agent.benchmarks --publish
```

<sub>Generated automatically by AI Code Review Agent Benchmark Engine • Duration: 15.060s</sub>