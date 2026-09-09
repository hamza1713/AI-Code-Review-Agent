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

# Bounded-score parameters (calibrated non-saturating scoring).
_WEIGHT = {"CRITICAL": 15, "HIGH": 10, "MEDIUM": 5, "LOW": 2, "INFO": 1}
_CEILING = {"CRITICAL": 65, "HIGH": 78, "MEDIUM": 88, "LOW": 95, "INFO": 98, "NONE": 100}
_FLOOR = {"CRITICAL": 15, "HIGH": 25, "MEDIUM": 45, "LOW": 65, "INFO": 85, "NONE": 100}
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
    "CWE-732": "HIGH",      # incorrect permission assignment (world-writable 0o777)
    "CWE-327": "MEDIUM",    # broken/weak crypto
    "CWE-326": "MEDIUM",    # inadequate encryption strength
    "CWE-330": "MEDIUM",    # weak randomness
    "CWE-20": "MEDIUM",     # improper input validation
    "CWE-703": "LOW",       # improper exception handling / silent swallow
    "CWE-390": "LOW",       # detection of error condition without action
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
    (re.compile(r"os\.chmod\s*\([^,]+,\s*(?:0o?777|0o?666)", re.I), "CWE-732"),
    (re.compile(r"except(?:\s+Exception)?\s*:\s*(?:\r?\n\+?\s*)?(?:pass|\.\.\.)", re.I), "CWE-703"),
    (re.compile(r"random\.(?:choice|random|randint|randrange)", re.I), "CWE-330"),
    (re.compile(r"check_hostname\s*=\s*False|verify_mode\s*=\s*ssl\.CERT_NONE|verify\s*=\s*False", re.I), "CWE-295"),
    (re.compile(r"open\s*\(\s*os\.path\.join", re.I), "CWE-22"),
]

# Governance rule_id keywords → the security CWE they proxy (else pure style).
_GOV_CWE_HINTS: List[Tuple[str, str]] = [
    ("raw-sql", "CWE-89"), ("sql", "CWE-89"),
    ("plaintext-password", "CWE-256"), ("password", "CWE-256"),
    ("eval", "CWE-95"), ("pickle", "CWE-502"), ("secret", "CWE-798"),
]

# Governance rule_id keywords that are production-only STYLE rules (rule 4 scoping).
_STYLE_RULE_KEYWORDS = ("print", "sleep", "wildcard", "import", "logging", "log", "todo", "naming", "format")

# Bandit import-blacklist tests (B401–B415): the *import* of a risky module is context,
# not a sink — it must never inherit the sink's severity. It is folded into the real use
# finding if one exists, else surfaced as INFO. Matched on the BANDIT-<id> rule id.
_IMPORT_BLACKLIST_IDS = tuple(f"B4{n:02d}" for n in range(1, 16))
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s")

# Low-confidence Bandit heuristics that should never escalate to a high severity:
# B603 (subprocess w/o shell=True), B607 (partial executable path), B110 (try/except/pass).
_LOW_CONFIDENCE_IDS = ("B603", "B607", "B110")


def _is_safe_subprocess(snippet: str) -> bool:
    """
    True if a subprocess snippet is not shell-injectable (list args or shell=False, no
    string interpolation). Kept inline so the reconciler stays free of the tools/crewai
    import chain; mirrors tools.bandit_runner.is_safe_subprocess_call.
    """
    text = snippet or ""
    if "os.system" in text or re.search(r"shell\s*=\s*True", text, re.I):
        return False
    if re.search(r"shell\s*=\s*False", text, re.I):
        return True
    if re.search(r"subprocess\.(?:call|run|Popen|check_output)\s*\(\s*\[", text):
        return True
    # No f-string / concatenation / %-format → Python defaults to shell=False (safe).
    return not re.search(r"f[\"']|\s\+\s|%s|%\s*\(|\.format\s*\(", text)

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
    rule_id: str = ""
    snippet: str = ""
    is_import: bool = False       # a risky *import* (context, not a sink)
    is_low_conf: bool = False     # a low-confidence Bandit heuristic (B603/B607/B110)


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
        # Risky imports are folded into the real use-finding they belong to (regardless of
        # line distance — imports sit at the top of the file, uses far below), so an import
        # never becomes its own inflated finding. Everything else clusters by adjacency.
        imports = [s for s in signals if s.is_import]
        clusters = cls._cluster([s for s in signals if not s.is_import])
        cls._fold_imports(clusters, imports)
        findings = [f for f in (cls._merge_cluster(c) for c in clusters) if f is not None]
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
            rid = (f.rule_id or "").upper()
            snippet = f.snippet or ""
            is_import = bool(_IMPORT_LINE.match(snippet)) or any(b in rid for b in _IMPORT_BLACKLIST_IDS)
            is_low_conf = any(b in rid for b in _LOW_CONFIDENCE_IDS)
            signals.append(_Signal(
                file=f.file_path, line=f.line_number, cwe=cwe,
                source=(f.analyzer_source or f.rule_id or "sast"),
                description=f.description, fix=f.fix_recommendation, raw_severity=f.severity,
                rule_id=rid, snippet=snippet, is_import=is_import, is_low_conf=is_low_conf,
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
    def _normalize_cwe(cwe: Optional[str]) -> Optional[str]:
        """Normalize CWE string so it consistently has exactly one CWE- prefix."""
        if not cwe:
            return None
        cleaned = cwe.strip().upper()
        while cleaned.startswith("CWE-"):
            cleaned = cleaned[4:]
        return f"CWE-{cleaned}" if cleaned else None

    @classmethod
    def _correct_cwe(cls, cwe: Optional[str], evidence: str) -> Optional[str]:
        """Rule 6: assign the CWE that matches the defect, correcting inherited mislabels."""
        for pattern, corrected in _CWE_CORRECTIONS:
            if pattern.search(evidence or ""):
                return corrected
        return cls._normalize_cwe(cwe)

    @classmethod
    def _signal_severity(cls, sig: _Signal) -> str:
        """Rule 2: ONE severity from the documented rubric (impact-based), not analyzer copy."""
        # A risky import is context, not a sink → never inherits the sink's severity.
        if sig.is_import:
            return "INFO"
        # Low-confidence Bandit heuristics (partial path, subprocess w/o shell) never escalate.
        if sig.is_low_conf:
            return "LOW"
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

    @staticmethod
    def _cluster_cwe(cluster: List[_Signal]) -> Optional[str]:
        """Canonical CWE of a cluster, preferring a known security CWE from its use (non-import) signals."""
        use_cwes = [s.cwe for s in cluster if s.cwe and not s.is_import]
        all_cwes = [s.cwe for s in cluster if s.cwe]
        return next(
            (c for c in use_cwes if c in _CWE_SEVERITY),
            next((c for c in all_cwes if c in _CWE_SEVERITY), (all_cwes[0] if all_cwes else None)),
        )

    @classmethod
    def _fold_imports(cls, clusters: List[List[_Signal]], imports: List[_Signal]) -> None:
        """Fold each risky import into the same-file, same-CWE use cluster it belongs to
        (regardless of distance). Imports with no matching use become their own INFO finding."""
        for imp in imports:
            target = None
            for c in clusters:
                if all(s.is_import for s in c):
                    continue  # don't fold one lone import into another
                if c and c[0].file == imp.file and imp.cwe and cls._cluster_cwe(c) == imp.cwe:
                    target = c
                    break
            if target is not None:
                target.append(imp)
            else:
                clusters.append([imp])

    @classmethod
    def _merge_cluster(cls, cluster: List[_Signal]) -> Optional[ReconciledFinding]:
        """Emit ONE consolidated finding, or None to drop a spurious low-confidence warning."""
        canonical_cwe = cls._cluster_cwe(cluster)
        non_import = [s for s in cluster if not s.is_import]

        if not non_import:
            # A lone risky import with no dangerous use in the diff — informational only.
            severity = "INFO"
        elif all(s.is_low_conf for s in non_import):
            # Low-confidence subprocess heuristics on a provably-safe call are spurious → drop.
            subprocess_lc = [s for s in non_import if any(b in s.rule_id for b in ("B603", "B607"))]
            if subprocess_lc and len(subprocess_lc) == len(non_import) and all(_is_safe_subprocess(s.snippet) for s in subprocess_lc):
                return None
            severity = "LOW"
        else:
            rep = _Signal(
                file=cluster[0].file, line=min(s.line for s in non_import), cwe=canonical_cwe,
                source="", description="", fix="", raw_severity=non_import[0].raw_severity,
                is_governance=all(s.is_governance for s in non_import),
                gov_severity=next((s.gov_severity for s in non_import if s.gov_severity), None),
            )
            severity = cls._signal_severity(rep)

        sources = sorted({s.source for s in cluster if s.source})
        line = min(s.line for s in (non_import or cluster))
        description = next((s.description for s in (non_import or cluster) if s.description), "Security/quality defect")
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
