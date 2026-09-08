"""
SynthesisReconciler — the deterministic final synthesis stage of the review pipeline.

It receives raw findings from multiple analyzers (regex scanner, Bandit/SAST, governance
engine) plus generated-test results and reconciles them into ONE coherent, honest report,
following a fixed contract:

  1. Deduplicate before counting or scoring. Findings that share a root cause are merged
     (keyed on file + CWE within an adjacency window, so an import-line + call-line pair or
     a same-line multi-rule hit collapse to ONE defect with a `sources` list). Every count
     equals the length of the final merged list.
  2. One severity per defect, assigned from a documented impact/exploitability rubric — not
     copied from whichever analyzer fired. The same defect never carries two severities.
  3. Honest test-evidence semantics. A generated test that asserts a vulnerability *is*
     present is `confirms_vulnerability`; a pass on it CONFIRMS the defect (bad news) and is
     never shown as a reassuring green badge. Only `confirms_fix` passes earn a positive one.
  4. Governance scoped to real production code (test/example/script/`__main__` paths are not
     flagged by production-only style rules), and blocking status matches true severity.
  5. A bounded, non-saturating quality score: a worst-severity ceiling minus diminishing
     per-defect penalties, floored so typical PRs stay informative. Every term traces to a
     listed finding.
  6. CWEs corrected to match the actual defect (e.g. eval()-on-input is CWE-95, not CWE-78).
  7. An explicit coverage/limitations note — this is pattern/Bandit-based, not dataflow/taint.

The reconciler is fully deterministic and operates only on structured analyzer output, so
the report's counts, severities, and score are guaranteed consistent — not merely requested.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from code_review_agent.models import SastFinding, RuleViolation, TestExecutionResult


# ── Canonical severity scale ────────────────────────────────────────────────
CANONICAL_SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]  # ascending
_SEV_RANK = {s: i for i, s in enumerate(CANONICAL_SEVERITIES)}

# Bounded-score parameters (documented in module docstring / README).
_WEIGHT = {"CRITICAL": 25, "HIGH": 15, "MEDIUM": 8, "LOW": 3, "INFO": 1}
_CEILING = {"CRITICAL": 60, "HIGH": 75, "MEDIUM": 88, "LOW": 95, "INFO": 98, "NONE": 100}
_FLOOR = {"CRITICAL": 5, "HIGH": 20, "MEDIUM": 40, "LOW": 60, "INFO": 85, "NONE": 100}
_DECAY = 0.5  # geometric decay → the k-th defect of a severity penalizes half as much

# Adjacency window (lines) within which same-root-cause signals merge (import + call, etc.).
_ADJACENCY = 3

# ── CWE → canonical severity (impact + exploitability), not analyzer default ─
_CWE_SEVERITY: Dict[str, str] = {
    "CWE-89": "CRITICAL",   # SQL injection
    "CWE-78": "CRITICAL",   # OS command injection
    "CWE-77": "CRITICAL",   # command injection
    "CWE-94": "CRITICAL",   # code injection
    "CWE-95": "CRITICAL",   # eval injection
    "CWE-502": "HIGH",      # insecure deserialization
    "CWE-798": "HIGH",      # hardcoded credentials
    "CWE-259": "HIGH",      # hardcoded password
    "CWE-256": "HIGH",      # plaintext password storage/compare
    "CWE-22": "HIGH",       # path traversal
    "CWE-79": "HIGH",       # XSS
    "CWE-611": "HIGH",      # XXE
    "CWE-918": "HIGH",      # SSRF
    "CWE-319": "HIGH",      # cleartext transmission
    "CWE-295": "HIGH",      # improper cert validation / disabled TLS verify
    "CWE-327": "MEDIUM",    # broken/weak crypto
    "CWE-326": "MEDIUM",    # inadequate encryption strength
    "CWE-330": "MEDIUM",    # weak randomness
}

# ── CWE correction rules (rule 6) — detect the real defect from evidence text ─
# Ordered: first match wins. Corrects inherited mislabels regardless of firing rule.
_CWE_CORRECTIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\beval\s*\(", re.I), "CWE-95"),
    (re.compile(r"\bexec\s*\(", re.I), "CWE-95"),
    (re.compile(r"pickle\.(?:loads?|load)\s*\(|yaml\.load\s*\((?!.*Safe)", re.I), "CWE-502"),
    (re.compile(r"os\.system\s*\(|subprocess\.[a-z_]+\([^)]*shell\s*=\s*True", re.I), "CWE-78"),
    (re.compile(r"hashlib\.(?:md5|sha1)\s*\(", re.I), "CWE-327"),
    (re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE)\b.*?(?:\{|\+|%\s*\()", re.I | re.S), "CWE-89"),
]

# Governance rule_id keywords → the security CWE they proxy (else pure style).
_GOV_CWE_HINTS: List[Tuple[str, str]] = [
    ("raw-sql", "CWE-89"), ("sql", "CWE-89"),
    ("plaintext-password", "CWE-256"), ("password", "CWE-256"),
    ("eval", "CWE-95"), ("pickle", "CWE-502"), ("secret", "CWE-798"),
]

# Governance rule_id keywords that are production-only STYLE rules (rule 4 scoping).
_STYLE_RULE_KEYWORDS = ("print", "sleep", "wildcard", "import", "logging", "log", "todo", "naming", "format")

# Path fragments that are NOT production code (production-only rules don't apply).
_NON_PROD_PATH = re.compile(
    r"(^|/)(tests?|test|examples?|samples?|scripts?|benchmarks?|fixtures?|conftest)(/|\.|_|$)"
    r"|_test\.|test_|\.spec\.",
    re.I,
)


@dataclass
class _Signal:
    """A single normalized analyzer signal before merging."""
    file: str
    line: int
    cwe: Optional[str]
    source: str
    description: str
    fix: str
    raw_severity: str
    is_governance: bool = False
    gov_severity: Optional[str] = None  # BLOCKING/WARNING/INFO for governance
    is_style_rule: bool = False


class ReconciledFinding(BaseModel):
    """ONE consolidated defect — the atomic unit everything else counts and scores from."""
    id: str
    title: str
    severity: str = Field(..., description="Canonical: CRITICAL/HIGH/MEDIUM/LOW/INFO")
    cwe: Optional[str] = None
    file: str
    line: int
    sources: List[str] = Field(default_factory=list)
    fix: str = ""
    blocking: bool = False
    rationale: str = ""


class TestEvidence(BaseModel):
    """Honest interpretation of the generated-test run (rule 3)."""
    kind: str = Field(default="none", description="confirms_vulnerability | confirms_fix | mixed | none")
    badge: str = Field(default="HEURISTIC", description="Honest badge label")
    positive: bool = Field(default=False, description="True only when a fix is verified")
    proves: str = Field(default="", description="Plain-English statement of what the result actually proves")


class ReconciledReport(BaseModel):
    """The final, internally consistent report the pipeline emits."""
    findings: List[ReconciledFinding] = Field(default_factory=list)
    findings_count: int = 0
    worst_severity: str = "NONE"
    score: int = 100
    score_breakdown: str = ""
    verdict: str = "APPROVE"
    blocking_reasons: List[str] = Field(default_factory=list)
    test_evidence: TestEvidence = Field(default_factory=TestEvidence)
    limitations_note: str = ""

    def to_markdown(self) -> str:
        """Render the reconciled report as a consistent Markdown section for delivery."""
        sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "⚪"}
        lines = [
            "## 🧮 Reconciled Findings (final synthesis)",
            "",
            f"**Verdict**: {self.verdict}  ·  **Quality Score**: {self.score}/100  ·  "
            f"**Findings**: {self.findings_count}  ·  **Worst severity**: {self.worst_severity}",
            "",
            f"*Score*: `{self.score_breakdown}`",
            "",
        ]
        if self.blocking_reasons:
            lines.append("### 🚫 Blocking")
            lines += [f"- {b}" for b in self.blocking_reasons]
            lines.append("")
        if self.findings:
            lines.append("| # | Severity | CWE | Location | Defect | Sources |")
            lines.append("| :-- | :-- | :-- | :-- | :-- | :-- |")
            for i, f in enumerate(self.findings, 1):
                icon = sev_icon.get(f.severity, "")
                loc = f"`{f.file}:L{f.line}`"
                safe_title = f.title.replace("|", "\\|")
                src = ", ".join(f.sources) or "—"
                lines.append(
                    f"| {i} | {icon} {f.severity} | {f.cwe or '—'} | {loc} | {safe_title} | {src} |"
                )
            lines.append("")
        else:
            lines.append("_No deduplicated defects were reconciled from the analyzers._\n")

        te = self.test_evidence
        lines.append(f"### 🧪 Empirical Test Evidence — {te.badge}")
        lines.append(f"> {te.proves}")
        lines.append("")
        lines.append(f"> ℹ️ {self.limitations_note}")
        return "\n".join(lines)

    def self_check(self) -> List[str]:
        """Return contract violations (empty == internally consistent). Used in tests and asserts."""
        problems: List[str] = []
        if self.findings_count != len(self.findings):
            problems.append(f"findings_count {self.findings_count} != len(findings) {len(self.findings)}")
        # No defect may carry two severities (ids are unique per defect here).
        seen = {}
        for f in self.findings:
            if f.id in seen and seen[f.id] != f.severity:
                problems.append(f"defect {f.id} has two severities: {seen[f.id]} and {f.severity}")
            seen[f.id] = f.severity
            if f.severity not in _WEIGHT:
                problems.append(f"defect {f.id} has non-canonical severity {f.severity}")
        # A passing/positive badge must never sit next to a confirmed vulnerability.
        if self.test_evidence.positive and self.test_evidence.kind == "confirms_vulnerability":
            problems.append("positive test badge on a confirms_vulnerability suite")
        # Verdict must be consistent with worst severity.
        if self.worst_severity == "CRITICAL" and self.verdict != "ESCALATE":
            problems.append("CRITICAL present but verdict is not ESCALATE")
        if self.worst_severity in ("LOW", "INFO", "NONE") and self.verdict == "ESCALATE" and not self.blocking_reasons:
            problems.append("verdict ESCALATE with only low-severity findings and no blocking reason")
        return problems


class SynthesisReconciler:
    """Deterministic reconciliation of analyzer findings + test results into one report."""

    @classmethod
    def reconcile(
        cls,
        sast_findings: Optional[List[SastFinding]] = None,
        rule_violations: Optional[List[RuleViolation]] = None,
        test_execution: Optional[TestExecutionResult] = None,
        generated_tests: str = "",
        pr_content: str = "",
    ) -> ReconciledReport:
        signals = cls._collect_signals(sast_findings or [], rule_violations or [], pr_content)
        clusters = cls._cluster(signals)
        findings = [cls._merge_cluster(c) for c in clusters]
        # Stable order: severity desc, then file, then line.
        findings.sort(key=lambda f: (-_SEV_RANK[f.severity], f.file, f.line))

        worst = cls._worst_severity(findings)
        score, breakdown = cls._score(findings, worst)
        verdict, blocking_reasons = cls._verdict(findings, worst)
        evidence = cls._interpret_tests(test_execution, generated_tests)

        report = ReconciledReport(
            findings=findings,
            findings_count=len(findings),
            worst_severity=worst,
            score=score,
            score_breakdown=breakdown,
            verdict=verdict,
            blocking_reasons=blocking_reasons,
            test_evidence=evidence,
            limitations_note=cls._limitations_note(),
        )
        return report

    # ── Signal collection & CWE/severity normalization ──────────────────────
    @classmethod
    def _collect_signals(cls, sast, governance, pr_content) -> List[_Signal]:
        main_guard = cls._main_guard_lines(pr_content)
        signals: List[_Signal] = []

        for f in sast:
            cwe = cls._correct_cwe(f.cwe, f"{f.description} {f.snippet} {f.rule_id}")
            signals.append(_Signal(
                file=f.file_path, line=f.line_number, cwe=cwe,
                source=(f.analyzer_source or f.rule_id or "sast"),
                description=f.description, fix=f.fix_recommendation, raw_severity=f.severity,
            ))

        for v in governance:
            rid = (v.rule_id or "").lower()
            gov_cwe = next((c for kw, c in _GOV_CWE_HINTS if kw in rid), None)
            is_style = any(kw in rid for kw in _STYLE_RULE_KEYWORDS) and gov_cwe is None
            # Rule 4: production-only style rules don't apply to non-production code.
            if is_style and (cls._non_production(v.file_path) or main_guard.get(v.file_path, 10**9) <= v.line_number):
                # Downgrade to INFO with reason rather than dropping — surfaced, not escalated.
                signals.append(_Signal(
                    file=v.file_path, line=v.line_number, cwe=None, source=v.rule_id,
                    description=f"{v.description} (scoped to INFO: not production code)",
                    fix=v.suggested_fix, raw_severity="INFO",
                    is_governance=True, gov_severity="INFO", is_style_rule=True,
                ))
                continue
            signals.append(_Signal(
                file=v.file_path, line=v.line_number, cwe=gov_cwe, source=v.rule_id,
                description=v.description, fix=v.suggested_fix, raw_severity=v.severity,
                is_governance=True, gov_severity=(v.severity or "WARNING").upper(), is_style_rule=is_style,
            ))
        return signals

    @staticmethod
    def _correct_cwe(cwe: Optional[str], evidence: str) -> Optional[str]:
        """Rule 6: assign the CWE that matches the defect, correcting inherited mislabels."""
        for pattern, corrected in _CWE_CORRECTIONS:
            if pattern.search(evidence or ""):
                return corrected
        return (cwe or "").strip().upper() or None

    @classmethod
    def _signal_severity(cls, sig: _Signal) -> str:
        """Rule 2: ONE severity from the documented rubric (impact-based), not analyzer copy."""
        if sig.cwe and sig.cwe in _CWE_SEVERITY:
            return _CWE_SEVERITY[sig.cwe]
        if sig.is_governance:
            gov = (sig.gov_severity or "WARNING").upper()
            # Blocking governance is a real must-fix (HIGH); warnings are low; info is info.
            return {"BLOCKING": "HIGH", "WARNING": "LOW", "INFO": "INFO"}.get(gov, "LOW")
        # Unknown-CWE security signal: fall back to a conservative normalization of its raw level.
        raw = (sig.raw_severity or "MEDIUM").upper()
        return raw if raw in _WEIGHT else "MEDIUM"

    # ── Deduplication / clustering (rule 1) ─────────────────────────────────
    @classmethod
    def _cluster(cls, signals: List[_Signal]) -> List[List[_Signal]]:
        """Union signals that share a root cause: same file, and same CWE (or an unknown CWE)
        within the adjacency window — collapsing multi-rule same-line and import+call pairs."""
        clusters: List[List[_Signal]] = []
        for sig in signals:
            placed = False
            for cluster in clusters:
                if any(cls._same_root_cause(sig, other) for other in cluster):
                    cluster.append(sig)
                    placed = True
                    break
            if not placed:
                clusters.append([sig])
        return clusters

    @staticmethod
    def _same_root_cause(a: _Signal, b: _Signal) -> bool:
        if a.file != b.file:
            return False
        if abs(a.line - b.line) > _ADJACENCY:
            return False
        # Same CWE, or one side's CWE unknown (import warning has no CWE but sits by the call).
        if a.cwe and b.cwe:
            return a.cwe == b.cwe
        return True

    @classmethod
    def _merge_cluster(cls, cluster: List[_Signal]) -> ReconciledFinding:
        """Emit ONE consolidated finding from a cluster of same-root-cause signals."""
        # Canonical CWE: prefer a known, specific security CWE.
        cwes = [s.cwe for s in cluster if s.cwe]
        canonical_cwe = next((c for c in cwes if c in _CWE_SEVERITY), (cwes[0] if cwes else None))

        # Rebuild a representative signal to derive the single severity from the rubric.
        rep = _Signal(
            file=cluster[0].file, line=min(s.line for s in cluster), cwe=canonical_cwe,
            source="", description="", fix="", raw_severity=cluster[0].raw_severity,
            is_governance=all(s.is_governance for s in cluster),
            gov_severity=next((s.gov_severity for s in cluster if s.gov_severity), None),
        )
        severity = cls._signal_severity(rep)

        sources = sorted({s.source for s in cluster if s.source})
        line = min(s.line for s in cluster)
        description = next((s.description for s in cluster if s.description), "Security/quality defect")
        fix = next((s.fix for s in cluster if s.fix), "Review and remediate the flagged code.")
        blocking = severity == "CRITICAL" or any(
            s.is_governance and (s.gov_severity == "BLOCKING") for s in cluster
        )

        fid = f"{cluster[0].file}:L{line}:{canonical_cwe or severity}"
        rationale = (
            f"Severity {severity} assigned from the impact/exploitability rubric for "
            f"{canonical_cwe or 'this defect class'} (analyzers reported: "
            f"{', '.join(sorted({s.raw_severity for s in cluster}))})."
        )
        return ReconciledFinding(
            id=fid, title=description[:120], severity=severity, cwe=canonical_cwe,
            file=cluster[0].file, line=line, sources=sources, fix=fix,
            blocking=blocking, rationale=rationale,
        )

    # ── Bounded, non-saturating score (rule 5) ──────────────────────────────
    @classmethod
    def _worst_severity(cls, findings: List[ReconciledFinding]) -> str:
        if not findings:
            return "NONE"
        return max((f.severity for f in findings), key=lambda s: _SEV_RANK[s])

    @classmethod
    def _score(cls, findings: List[ReconciledFinding], worst: str) -> Tuple[int, str]:
        ceiling = _CEILING[worst]
        if not findings:
            return 100, "No deduplicated findings → score 100 (ceiling, no penalties)."

        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1

        terms = []
        total_penalty = 0.0
        for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
            n = counts.get(sev, 0)
            if not n:
                continue
            # Diminishing: w * 2 * (1 - decay^n)  — bounded by 2w regardless of n.
            penalty = _WEIGHT[sev] * 2 * (1 - _DECAY ** n)
            total_penalty += penalty
            terms.append(f"-{penalty:.1f} ({n}x {sev})")

        raw = ceiling - total_penalty
        score = max(_FLOOR[worst], round(raw))
        breakdown = (
            f"Ceiling {ceiling} (worst: {worst}) " + " ".join(terms)
            + f" = {round(raw)}"
            + (f" (floored to {score})" if score != round(raw) else "")
        )
        return score, breakdown

    # ── Verdict (rule 4: low severity never drives the top line) ─────────────
    @classmethod
    def _verdict(cls, findings: List[ReconciledFinding], worst: str) -> Tuple[str, List[str]]:
        blocking_reasons = [
            f"{f.severity} {f.cwe or ''} in {f.file}:L{f.line} — {f.title}".strip()
            for f in findings if f.blocking
        ]
        if worst == "CRITICAL" or blocking_reasons:
            return "ESCALATE", blocking_reasons
        if worst in ("HIGH", "MEDIUM"):
            return "REQUEST CHANGES", blocking_reasons
        # LOW / INFO / NONE → approve; low-severity items are recommendations, not blockers.
        return "APPROVE", blocking_reasons

    # ── Test-evidence semantics (rule 3) ────────────────────────────────────
    @classmethod
    def _interpret_tests(cls, te: Optional[TestExecutionResult], source: str) -> TestEvidence:
        if te is None or not getattr(te, "executed", False) or (te.status == "SKIPPED"):
            return TestEvidence(
                kind="none", badge="⚪ HEURISTIC", positive=False,
                proves="No generated test was executed for these findings (static-analysis signal only).",
            )

        # Intent per the generator's convention (see tasks.yaml): current-behavior tests
        # assert the bug (often via pytest.raises); fix tests are marked "POST-FIX:".
        has_fix = bool(re.search(r"POST-FIX", source or "", re.I))
        has_vuln_assertion = bool(re.search(r"pytest\.raises", source or ""))
        if has_fix and not has_vuln_assertion:
            kind = "confirms_fix"
        elif has_fix and has_vuln_assertion:
            kind = "mixed"
        else:
            # No fix marker → the suite asserts current (possibly buggy) behavior.
            kind = "confirms_vulnerability"

        badge_in = te.evidence_badge
        if badge_in in ("REPRODUCED",) or te.status == "REPRODUCED_DEFECT":
            # A predicted defect assertion fired — the vulnerability reproduced.
            return TestEvidence(
                kind="confirms_vulnerability", badge="🔴 VULNERABILITY CONFIRMED", positive=False,
                proves="A generated test reproduced the defect on the actual PR code — this confirms the vulnerability; it is not a passing/safe signal.",
            )
        if badge_in in ("PASSING", "PASSED") or te.status == "PASSED":
            if kind == "confirms_fix":
                return TestEvidence(
                    kind="confirms_fix", badge="🟢 FIX VERIFIED", positive=True,
                    proves="A post-fix regression test passed — the proposed fix behaves as intended.",
                )
            # A pass on a current-behavior (confirms-vulnerability) suite confirms the defect.
            return TestEvidence(
                kind="confirms_vulnerability", badge="🔴 VULNERABILITY CONFIRMED", positive=False,
                proves="The suite asserts the current (buggy) behavior and passed — this CONFIRMS the defect is present. A green 'passing' here would be misleading.",
            )
        # ERROR / TIMEOUT / anything else → not proven either way.
        return TestEvidence(
            kind=kind, badge="🟡 UNVERIFIED", positive=False,
            proves="The generated test could not be run to a conclusive result (syntax error, missing dependency, or timeout) — the finding stands on static analysis alone.",
        )

    # ── Governance production-scoping helpers (rule 4) ──────────────────────
    @staticmethod
    def _non_production(path: str) -> bool:
        return bool(_NON_PROD_PATH.search(path or ""))

    @staticmethod
    def _main_guard_lines(pr_content: str) -> Dict[str, int]:
        """Best-effort: first added-line number of an `if __name__ == '__main__':` per file.
        Findings in that file at or below this line are treated as `__main__` scope."""
        result: Dict[str, int] = {}
        if not pr_content:
            return result
        current_file = None
        new_line = 0
        guard = re.compile(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')
        for raw in pr_content.splitlines():
            if raw.startswith("+++ b/"):
                current_file = raw[6:].strip()
                new_line = 0
                continue
            if raw.startswith("@@"):
                m = re.search(r"\+(\d+)", raw)
                new_line = (int(m.group(1)) - 1) if m else new_line
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                new_line += 1
                if current_file and current_file not in result and guard.search(raw):
                    result[current_file] = new_line
            elif not raw.startswith("-"):
                new_line += 1
        return result

    @staticmethod
    def _limitations_note() -> str:
        return (
            "Coverage & limitations: this pipeline is pattern/Bandit-based static analysis with an "
            "AST call-graph — it has no dataflow/taint tracking. It reliably flags direct, single-"
            "site sinks (raw SQL, os.system/eval, pickle, weak hashes, hardcoded secrets) but "
            "cannot reliably detect defects that require following data across calls or state: "
            "second-order/stored injection, SSRF, disabled TLS verification / SSLContext downgrades, "
            "many path-traversal variants, auth/authorization logic flaws, and race conditions. "
            "Absence of a finding here is not proof of safety — treat this as one layer, not a full audit."
        )
