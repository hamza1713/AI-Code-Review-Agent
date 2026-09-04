---
name: ai-code-review-agent-skills
description: >
  Complete standardized Agent Skills catalog for the AI Code Review Agent (Enterprise Edition v2.0).
  Defines 12 skills across 3 crew agents (Senior Developer, Security Engineer, Tech Lead)
  and the Flow orchestration layer. Each skill follows the Agent Skills Specification v1.0
  standard: YAML frontmatter + activation triggers + procedural protocol + guardrails.
version: 2.0.0
author: AI Code Review System
framework: CrewAI Flows 2.0
---

# 🛠️ AI Code Review Agent — Agent Skills Specification (`skills.md`)

> **Standard:** Agent Skills Specification v1.0 (YAML Frontmatter + Procedural Instructions)  
> **Framework:** CrewAI Flows 2.0 · Google Gemini 2.5  
> **Project:** AI Code Review Agent (Enterprise Edition v2.0)

---

## 📑 Table of Contents

1. [Skill Architecture & Agent-Skill Assignment Map](#1-skill-architecture--agent-skill-assignment-map)
2. [Senior Developer Skills](#-senior-developer-agent-skills)
   - [Skill A: `senior-dev-quality-reviewer`](#skill-a-senior-dev-quality-reviewer)
   - [Skill B: `ast-callgraph-context-indexer`](#skill-b-ast-callgraph-context-indexer)
   - [Skill C: `api-breaking-change-detector`](#skill-c-api-breaking-change-detector)
   - [Skill D: `performance-and-concurrency-auditor`](#skill-d-performance-and-concurrency-auditor)
3. [Security Engineer Skills](#-security-engineer-agent-skills)
   - [Skill E: `sast-vulnerability-auditor`](#skill-e-sast-vulnerability-auditor)
   - [Skill F: `git-diff-and-patch-analyzer`](#skill-f-git-diff-and-patch-analyzer)
4. [Tech Lead Skills](#-tech-lead-agent-skills)
   - [Skill G: `tech-lead-verdict-synthesizer`](#skill-g-tech-lead-verdict-synthesizer)
   - [Skill H: `automated-unit-test-generator`](#skill-h-automated-unit-test-generator)
   - [Skill I: `governance-policy-enforcer`](#skill-i-governance-policy-enforcer)
5. [Flow-Level Skills (Orchestration Layer)](#-flow-level-skills-orchestration-layer)
   - [Skill J: `sarif-v21-compliance-exporter`](#skill-j-sarif-v21-compliance-exporter)
   - [Skill K: `github-pr-comment-generator`](#skill-k-github-pr-comment-generator)
   - [Skill L: `telemetry-and-cost-optimizer`](#skill-l-telemetry-and-cost-optimizer)
6. [Complete Skill Execution Matrix](#6-complete-skill-execution-matrix)

---

## 1. Skill Architecture & Agent-Skill Assignment Map

### Skills vs. Tools — Key Distinction

| Concept | What It Is | Examples in This Project |
| :--- | :--- | :--- |
| **Skill** | Procedural reasoning protocol loaded into agent context | `sast-vulnerability-auditor`, `tech-lead-verdict-synthesizer` |
| **Tool** | Executable function the agent can call | `CodebaseContextTool`, `RuffTool`, `QuickPatternScannerTool` |

Skills teach agents **how to reason**. Tools give agents **actions to take**.

### Agent → Skill → Tool Assignment Table

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              AGENT SKILL & TOOL ASSIGNMENT MATRIX                                                    │
├─────────────────────────┬────────────────────────────────────────────────────────┬───────────────────────────────────┤
│ 👨‍💻 SENIOR DEVELOPER      │ 🔐 SECURITY ENGINEER                                   │ 👑 TECH LEAD                      │
│ agent: senior_developer │ agent: security_engineer                               │ agent: tech_lead                  │
├─────────────────────────┼────────────────────────────────────────────────────────┼───────────────────────────────────┤
│ SKILLS:                 │ SKILLS:                                                │ SKILLS:                           │
│ [A] senior-dev-quality- │ [E] sast-vulnerability-auditor                         │ [G] tech-lead-verdict-synthesizer │
│     reviewer            │     → Apply OWASP Top 10 + CWE classification          │     → Confidence rubric + verdict  │
│ [B] ast-callgraph-      │     → Multi-tool deduplication (Semgrep+Bandit+Regex)  │ [H] automated-unit-test-generator │
│     context-indexer     │     → Secure replacement code formulation              │     → Pytest suite generation     │
│ [C] api-breaking-       │ [F] git-diff-and-patch-analyzer                        │ [I] governance-policy-enforcer    │
│     change-detector     │     → Added-line (+) only scanning                     │     → .code-review.yaml rules     │
│ [D] performance-and-    │     → Hunk line mapping for precise locations           │                                   │
│     concurrency-auditor │                                                        │                                   │
├─────────────────────────┼────────────────────────────────────────────────────────┼───────────────────────────────────┤
│ TOOLS:                  │ TOOLS:                                                 │ TOOLS:                            │
│ ✅ CodebaseContextTool   │ ✅ QuickPatternScannerTool                              │ ✅ CustomRulesTool                 │
│    (AST call graph)     │    (Semgrep + Bandit + Regex unified scanner)          │    (.code-review.yaml evaluator)  │
│ ✅ RuffTool              │ ✅ SerperDevTool                                        │ ✅ TestGeneratorTool               │
│    (fast linter)        │    (CVE/OWASP web search — optional, needs API key)    │    (pytest AST generator)         │
│                         │ ✅ ScrapeWebsiteTool                                    │                                   │
│                         │    (vulnerability detail scraping — optional)          │                                   │
├─────────────────────────┼────────────────────────────────────────────────────────┼───────────────────────────────────┤
│ TASK: analyze_code_     │ TASK: review_security                                  │ TASK: summarize_findings          │
│ quality (async)         │ (async, parallel with Senior Dev)                      │ (sequential, after both complete) │
│ Output: CodeQualityJSON │ Output: ReviewSecurityJSON + Guardrail validation      │ Output: SummarizedFindingsJSON    │
└─────────────────────────┴────────────────────────────────────────────────────────┴───────────────────────────────────┘
```

---

## 👨‍💻 Senior Developer Agent Skills

---

### Skill A: `senior-dev-quality-reviewer`

```yaml
---
name: senior-dev-quality-reviewer
description: >
  Evaluates software architecture, maintainability, design patterns, cognitive complexity,
  Ruff linter findings, and inline suggestion generation for pull request diffs.
version: 2.0.0
assigned_to_agent: senior_developer
assigned_tools:
  - RuffTool          # Run Ruff fast linter on modified Python files
  - CodebaseContextTool  # Look up symbol definitions for context
task: analyze_code_quality
outputs:
  - critical_issues: list[str]
  - minor_issues: list[str]
  - reasoning: str
  - inline_suggestions: list[InlineComment]
---
```

#### 🎯 Activation Trigger
Activate on **every** `analyze_code_quality` task execution. This is the primary quality reasoning protocol for the Senior Developer agent.

#### 📋 Step-by-Step Reasoning Protocol

1. **Architectural Integrity**
   - Flag monolithic methods exceeding 50 lines of logic (not including docstrings/comments).
   - Identify classes with more than one unrelated responsibility (SRP violation).
   - Look for cyclic imports, deep inheritance hierarchies (> 3 levels), or god-class anti-patterns.

2. **Error Handling & Exception Hygiene**
   - Flag bare `except:` clauses with no exception type specified.
   - Flag swallowed exceptions where `except Exception: pass` or similar patterns discard errors silently.
   - Verify `finally` blocks exist for file handles, database connections, and socket operations.
   - For async code, verify `asyncio.gather` uses `return_exceptions=True` or equivalent error handling.

3. **Resource Lifecycle Management**
   - Verify all file opens use `with open(...) as f:` context managers.
   - Verify database connections are obtained via context managers or connection pools.
   - Check for missing `close()` calls on resources not in context managers.

4. **Boundary & Input Validation**
   - Verify public function parameters have null/None checks.
   - Verify string parameters have empty-string guards where applicable.
   - Verify numeric inputs have range bounds validation where semantically required.

5. **Ruff Linting** (via `RuffTool`)
   - Execute Ruff on modified Python files extracted from the diff.
   - Map Ruff findings back to original line numbers using hunk offsets.
   - Classify Ruff findings: `critical_issues` for `E` (errors), `minor_issues` for `W` (warnings) and style codes.

6. **Inline Suggestion Formulation**
   - For every `critical_issue` and `minor_issue`, produce an `inline_suggestion` with:
     - Exact `path` (relative file path from diff header).
     - `line` (new file line number, not diff-relative).
     - `severity`: `CRITICAL`, `WARNING`, or `INFO`.
     - `comment_body`: Terse technical explanation of the problem.
     - `suggestion_code`: A complete, copy-pasteable replacement code block.

---

### Skill B: `ast-callgraph-context-indexer`

```yaml
---
name: ast-callgraph-context-indexer
description: >
  Uses the pre-built repository AST call graph to identify which external callers
  may be impacted by modified functions or changed signatures in the PR diff.
version: 2.0.0
assigned_to_agent: senior_developer
assigned_tools:
  - CodebaseContextTool  # query_type: find_callers / analyze_diff_impact
task: analyze_code_quality
outputs:
  - cross_file_risks: list[str]
---
```

#### 🎯 Activation Trigger
Activate when the diff modifies **function signatures**, **class method definitions**, **return types**, or **shared utility modules** used across multiple files.

#### 📋 Step-by-Step Reasoning Protocol

1. **Identify Modified Symbols**: Read `code_graph_context` injected by the Flow orchestrator. This pre-computed context already lists which functions were modified and which external callers they have.

2. **Reason About Impact**: For each listed caller:
   - Determine if the modified function's **signature changed** (added/removed parameters, changed return type).
   - Determine if the modified function's **behavior changed** in a way that could break assumptions in the caller.
   - Assess if the caller handles the new behavior correctly.

3. **Formulate `cross_file_risks`**: Produce one string entry per impacted caller, formatted as:
   `"Function 'X' in file.py is called by Y callers. Callers in [caller_file.py:L{line}] may break if they pass the removed 'param' argument."`

4. **Tool Usage** (if `code_graph_context` is insufficient):
   - Call `CodebaseContextTool` with `query_type: find_callers` and the function name to get an up-to-date caller list.
   - Call `CodebaseContextTool` with `query_type: lookup_symbol` to retrieve the current signature.

---

### Skill C: `api-breaking-change-detector`

```yaml
---
name: api-breaking-change-detector
description: >
  Audits modified API routes, endpoint handlers, Pydantic/dataclass schemas, and
  function signatures for backwards-incompatible changes that would break downstream
  consumers (clients, microservices, mobile apps).
version: 2.0.0
assigned_to_agent: senior_developer
assigned_tools: []   # Reasoning-only skill, no tool calls required
task: analyze_code_quality
outputs:
  - critical_issues: list[str]   # Breaking changes → critical_issue entries
  - minor_issues: list[str]      # Non-breaking deprecation warnings → minor_issue entries
---
```

#### 🎯 Activation Trigger
Activate when the diff modifies **FastAPI/Flask routes**, **Pydantic models**, **dataclasses used in API responses**, **function signatures of public module APIs**, or **GraphQL schemas**.

#### 📋 What Constitutes a Breaking Change

| Change Type | Severity |
| :--- | :--- |
| Removed route/endpoint | `CRITICAL` |
| Renamed route path | `CRITICAL` |
| Removed required request parameter | `CRITICAL` |
| Removed field from response schema | `CRITICAL` |
| Changed HTTP method (GET → POST) | `CRITICAL` |
| Changed HTTP status code for success path | `CRITICAL` |
| Added new **required** request parameter | `CRITICAL` |
| Changed field type in response schema | `HIGH` |
| Removed optional field | `WARNING` |
| Renamed field without alias | `WARNING` |
| Added new optional parameter with default | `INFO` (non-breaking) |
| Added new optional response field | `INFO` (non-breaking) |

---

### Skill D: `performance-and-concurrency-auditor`

```yaml
---
name: performance-and-concurrency-auditor
description: >
  Identifies performance bottlenecks and concurrency safety violations including
  N+1 query patterns, blocking I/O inside async functions, thread race conditions,
  memory leaks, and uncontrolled retry storms.
version: 2.0.0
assigned_to_agent: senior_developer
assigned_tools: []   # Reasoning-only skill, no tool calls required
task: analyze_code_quality
outputs:
  - critical_issues: list[str]   # Deadlocks, data races, unbounded memory
  - minor_issues: list[str]      # N+1 queries, hardcoded sleeps, sync in async
---
```

#### 🎯 Activation Trigger
Activate when the diff contains ORM queries inside loops, `async def` functions, threading primitives (`Lock`, `Thread`, `Queue`), or large collection transformations.

#### 📋 Anti-Patterns to Detect

1. **N+1 Query Pattern**: A database query inside a `for` loop that could be replaced by a single `select_related()`, `prefetch_related()`, or `JOIN`.
2. **Blocking I/O in Async Context**: `requests.get()`, `open()`, `time.sleep()`, or synchronous DB calls inside `async def` without `await` — blocks the event loop.
3. **Hardcoded `time.sleep()`**: Unexplained fixed sleeps instead of event-driven waiting or exponential backoff.
4. **Unprotected Shared Mutable State**: Class variables or module-level mutable dicts/lists modified from multiple threads without a `Lock`.
5. **Unbounded Memory Growth**: Appending to a list/dict inside an infinite loop or long-running process without eviction or size bounds.
6. **Retry Storms**: Catch-all retry loops without exponential backoff and jitter that could flood a downstream service.

---

## 🔐 Security Engineer Agent Skills

---

### Skill E: `sast-vulnerability-auditor`

```yaml
---
name: sast-vulnerability-auditor
description: >
  Audits code changes for security vulnerabilities per OWASP Top 10 and CWE taxonomy.
  Merges findings from Semgrep, Bandit, and regex heuristic scanners (pre-computed
  in sast_context) using strict deduplication rules. Produces structured vulnerability
  entries with evidence and secure replacement code.
version: 2.0.0
assigned_to_agent: security_engineer
assigned_tools:
  - QuickPatternScannerTool   # Unified Semgrep + Bandit + Regex scanner
  - SerperDevTool             # Search CVE databases & OWASP advisories (optional)
  - ScrapeWebsiteTool         # Scrape vulnerability detail pages (optional)
task: review_security
outputs:
  - security_vulnerabilities: list[SecurityVulnerability]
  - blocking: bool
  - highest_risk: str
  - security_recommendations: list[str]
  - inline_security_comments: list[InlineComment]
---
```

#### 🎯 Activation Trigger
Activate on every `review_security` task execution. This is the primary security reasoning protocol for the Security Engineer agent.

#### 📋 Threat Coverage Taxonomy

| Vulnerability | CWE | Pattern / Indicator | Fix Protocol |
| :--- | :--- | :--- | :--- |
| SQL Injection | CWE-89 | `f"SELECT ... {var}"`, `"..." + var` in DB calls | Parameterized queries: `db.query("SQL", (param,))` |
| Plaintext Password Comparison | CWE-256 | `password == user.password`, direct string compare | `bcrypt.checkpw(plain.encode(), hash.encode())` |
| Hardcoded Secret / API Key | CWE-798 | `api_key = "abc123..."` 16+ alphanumeric chars | Move to env var: `os.getenv("API_KEY")` |
| OS Command Injection | CWE-78 | `os.system(...)`, `subprocess.run(shell=True, ...)` | `subprocess.run(["cmd", arg], shell=False)` |
| Insecure Deserialization | CWE-502 | `pickle.loads(...)`, `yaml.load(...)` (no SafeLoader) | Use `json.loads()` or `yaml.safe_load()` |
| Weak Cryptography | CWE-327 | `hashlib.md5(...)`, `hashlib.sha1(...)` | Use `hashlib.sha256()` or `hashlib.sha3_256()` |
| Cross-Site Scripting | CWE-79 | Unescaped user input in HTML template strings | Use template auto-escaping (Jinja2 `autoescape=True`) |
| Path Traversal | CWE-22 | `open(user_input)`, `os.path.join` with user input | Validate against allowed base directory with `Path.resolve()` |

#### 📋 Mandatory Deduplication Protocol
Before writing any `security_vulnerabilities` entry:
1. Check if the `sast_context` (pre-computed findings) contains multiple entries with the **same file path**, **overlapping line range** (within ±3 lines), and **same root cause** (e.g. both flag the same f-string SQL query).
2. If yes → create a **single** merged entry with all contributing rule IDs listed in `matched_by`.
3. Set `evidence` to the code snippet from the most precise finding (prefer Semgrep/Bandit over regex).
4. **Never** produce two entries for the same vulnerability instance.

#### 📋 `highest_risk` Derivation Rule (MANDATORY for guardrail)
The `highest_risk` field **must** equal the maximum `risk_level` across all `security_vulnerabilities` entries:
```
if any vuln.risk_level == "critical" → highest_risk = "critical"
elif any vuln.risk_level == "high"   → highest_risk = "high"
elif any vuln.risk_level == "medium" → highest_risk = "medium"
elif any vuln.risk_level == "low"    → highest_risk = "low"
else (no vulnerabilities)            → highest_risk = "none"
```
Failure to match this rule will cause the output guardrail to reject the response and request a retry.

---

### Skill F: `git-diff-and-patch-analyzer`

```yaml
---
name: git-diff-and-patch-analyzer
description: >
  Interprets unified git diff format to extract only added lines for security scanning,
  correctly maps line numbers from hunk headers, and filters non-actionable generated
  or lock files from the review scope.
version: 2.0.0
assigned_to_agent: security_engineer
assigned_tools: []   # Reasoning-only skill, applied during diff interpretation
task: review_security
outputs:
  - Informs which lines to scan (added lines only)
  - Provides accurate file:line locations for inline_security_comments
---
```

#### 🎯 Activation Trigger
Activate when reasoning about **which lines to scan** and when **determining exact line numbers** for inline security comments.

#### 📋 Diff Interpretation Rules

1. **Line Prefixes**:
   - `+` prefix → **Added line** (new code in PR) → **SCAN THIS LINE**
   - `-` prefix → **Deleted line** (removed in PR) → **DO NOT flag** (code no longer exists)
   - ` ` prefix (space) → **Context line** (unchanged) → DO NOT flag (not part of this PR)
   - `@@` line → **Hunk header** — extract `new_start` line number from `+{start},{len}` field

2. **Line Number Calculation**:
   - From hunk header `@@ -old_start,old_len +new_start,new_len @@`, `new_start` is the absolute line number of the first line in the new file.
   - Count forward through context (` `) and added (`+`) lines to derive absolute new-file line numbers.
   - Use these absolute line numbers in `inline_security_comments[].line` — not diff-relative numbers.

3. **File Exclusions** — Do NOT scan findings in:
   - `package-lock.json`, `yarn.lock`, `poetry.lock`, `Pipfile.lock` (dependency locks)
   - `*.min.js`, `*.min.css` (minified assets)
   - Generated migration files (e.g. `**/migrations/0001_*.py`)
   - Binary files (PDF, images, compiled artifacts)

---

## 👑 Tech Lead Agent Skills

---

### Skill G: `tech-lead-verdict-synthesizer`

```yaml
---
name: tech-lead-verdict-synthesizer
description: >
  Synthesizes code quality, security, and governance analyses into an authoritative
  merge decision using a deterministic mathematical confidence scoring rubric.
  Enforces strict provenance: every finding must trace to upstream agent output.
version: 2.0.0
assigned_to_agent: tech_lead
assigned_tools: []   # Reasoning-only; uses CustomRulesTool for governance context
task: summarize_findings
outputs:
  - confidence: int (0-100)
  - confidence_breakdown: str
  - findings: str
  - coverage_gaps: list[str]
  - fix: list[Fix]
  - recommendations: list[str]
  - inline_comments: list[InlineComment]
---
```

#### 🎯 Activation Trigger
Activate on every `summarize_findings` task execution. This is the primary synthesis protocol for the Tech Lead agent.

#### 📐 Deterministic Confidence Scoring Rubric (MANDATORY)

```
Starting score: 100

For each unresolved vulnerability in review_security output:
  - risk_level == "critical" → -30 per instance
  - risk_level == "high"     → -15 per instance
  - risk_level == "medium"   → -5  per instance
  - risk_level == "low"      → -5  per instance

For each item in analyze_code_quality output:
  - critical_issue → -10 per instance
  - minor_issue    → -5  per instance

For each item in rules_context (governance violations):
  - severity == "BLOCKING" → -10 per instance
  - severity == "WARNING"  → -5  per instance

Floor = 0 (score cannot go below 0)

confidence_breakdown must show all arithmetic, e.g.:
"100 - 30 (critical: SQLi in auth.py:L45) - 15 (high: hardcoded token in config.py:L12)
 - 10 (critical_issue: bare except in handler.py:L78) - 5 (gov-no-print: WARNING) = 40"
```

#### 📋 Verdict Decision Tree

```
IF any vulnerability.risk_level == "critical"
   OR any governance rule severity == "BLOCKING"
     → verdict = "ESCALATE"

ELSE IF confidence < 85
      OR len(fix) > 0  (actionable defects exist)
      OR any governance rule severity == "WARNING"
     → verdict = "REQUEST CHANGES"

ELSE (confidence >= 85 AND no blocking defects)
     → verdict = "APPROVE"
```

#### 🛡️ Anti-Hallucination Provenance Rule (MANDATORY)
- Every item in `findings`, `fix`, `recommendations`, and `inline_comments` **MUST** directly reference a specific item from `analyze_code_quality` output or `review_security` output that was provided as context.
- If you believe an additional issue exists but wasn't reported upstream, place it in `coverage_gaps` — **NEVER** add it as a finding or fix.
- If `analyze_code_quality` reported zero issues, **explicitly state** `"Code Quality: No critical or minor issues identified by Senior Developer review."` in `findings` — do not omit this section.

---

### Skill H: `automated-unit-test-generator`

```yaml
---
name: automated-unit-test-generator
description: >
  Generates complete, executable pytest test suites covering modified functions,
  edge cases, error conditions, and post-fix regression validations using AST
  function signature introspection.
version: 2.0.0
assigned_to_agent: tech_lead
assigned_tools:
  - TestGeneratorTool   # AST-based pytest scaffold generator
task: summarize_findings
outputs:
  - suggested_unit_tests: str  (complete pytest source code)
---
```

#### 🎯 Activation Trigger
Always activate during `summarize_findings` to provide immediate test scaffolding alongside the review.

#### 📋 Test Generation Protocol

1. **Target Identification**: Extract all function/method signatures from `{file_content}` that appear in added lines of the diff. Use `TestGeneratorTool` with `function_signature` = the full def block.

2. **Test Categories to Generate**:
   - **Happy Path**: Standard valid inputs → expected return value assertion.
   - **Boundary Tests**: Empty string, `None`, `0`, `-1`, max integer, empty list, single-element list.
   - **Error/Exception Tests**: Assert `pytest.raises(ExceptionType)` for currently-buggy behavior. Do NOT assert the fixed behavior here.
   - **Type Validation**: If type annotations exist, test that wrong types raise `TypeError` or `ValueError`.
   - **Async Tests**: Mark with `@pytest.mark.asyncio` and use `await` for async functions.

3. **POST-FIX Separation Rule**: Tests that validate a *proposed fix* (not current behavior) must:
   - Be in a **separate** test function.
   - Have their docstring prefixed with `POST-FIX:`.
   - **Never** be mixed with current-behavior tests in the same function.

4. **Import Requirements**: Always include at minimum: `import pytest`, the module import for the function under test, and any `unittest.mock.patch` or `MagicMock` imports for external dependencies.

---

### Skill I: `governance-policy-enforcer`

```yaml
---
name: governance-policy-enforcer
description: >
  Ensures all violations from the .code-review.yaml governance rules engine
  (provided in rules_context) are fully reflected in the final confidence score,
  verdict, and fix items. Prevents governance findings from being silently omitted.
version: 2.0.0
assigned_to_agent: tech_lead
assigned_tools:
  - CustomRulesTool   # Queries .code-review.yaml rules and evaluates the diff
task: summarize_findings
outputs:
  - Contributes to: confidence deduction (-10 per BLOCKING, -5 per WARNING)
  - Contributes to: verdict (ESCALATE on BLOCKING, REQUEST CHANGES on WARNING)
  - Contributes to: fix items for each BLOCKING violation
  - Contributes to: recommendations for each WARNING violation
---
```

#### 🎯 Activation Trigger
Activate when `rules_context` contains any governance rule violations. Always apply when synthesizing the final verdict.

#### 📋 Governance Integration Rules

1. **BLOCKING violations** (e.g. `gov-no-raw-sql`, `gov-no-plaintext-passwords`):
   - Automatically force verdict → `ESCALATE`.
   - Create a `fix` entry for each BLOCKING violation with `solutions` = the `suggested_fix` from the rule.
   - Deduct **10 points** from confidence per BLOCKING violation.

2. **WARNING violations** (e.g. `gov-no-print-statements`, `gov-no-hardcoded-sleep`, `gov-no-wildcard-imports`):
   - Contribute to `REQUEST CHANGES` verdict if no critical security issues already force `ESCALATE`.
   - Add to `recommendations` (not `fix`): `"[gov-no-print-statements] Replace print() with logger.info() or logger.debug() to prevent stdout pollution in production."`
   - Deduct **5 points** from confidence per WARNING violation.

3. **INFO violations**:
   - Add to `recommendations` as a best-practice note.
   - No confidence deduction.
   - No verdict impact.

---

## 🔄 Flow-Level Skills (Orchestration Layer)

These skills are executed by the **CrewAI Flow orchestrator** (`main.py`) — **not** by the crew agents — but are documented here as skills because they represent specialized procedural capabilities.

---

### Skill J: `sarif-v21-compliance-exporter`

```yaml
---
name: sarif-v21-compliance-exporter
description: >
  Serializes SAST findings and inline review comments into OASIS SARIF v2.1.0
  standard JSON format for GitHub Code Scanning, SonarQube, and DefectDojo.
version: 2.0.0
assigned_to_agent: flow_orchestrator   # PRCodeReviewFlow.make_final_decision()
assigned_tools:
  - SarifExporter   # src/code_review_agent/sarif_exporter.py
task: make_final_decision (flow step, not crew task)
activation: When sarif_output_path is set in ReviewState
---
```

#### 📋 SARIF v2.1.0 Compliance Requirements
- **`$schema`** must be `https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json`
- **`version`** must be `"2.1.0"`
- Every **rule** must have: `id`, `name`, `shortDescription.text`, `fullDescription.text`, `defaultConfiguration.level` (`error`|`warning`|`note`)
- Every **result** must have: `ruleId`, `level`, `message.text`, `locations[0].physicalLocation.artifactLocation.uri`, `locations[0].physicalLocation.region.startLine`
- Severity mapping: `CRITICAL`|`HIGH` → `error`, `MEDIUM` → `warning`, `LOW`|`INFO` → `note`

---

### Skill K: `github-pr-comment-generator`

```yaml
---
name: github-pr-comment-generator
description: >
  Formats inline review comments and posts them to GitHub REST API using
  1-click suggestion diff blocks (```suggestion markdown).
version: 2.0.0
assigned_to_agent: flow_orchestrator   # PRCodeReviewFlow.make_final_decision()
assigned_tools:
  - GitHubClient   # src/code_review_agent/github_client.py
task: make_final_decision (flow step)
activation: When pr_url is set AND GITHUB_TOKEN is configured
---
```

#### 📋 GitHub Inline Comment Format
```markdown
⚠️ **WARNING**: Formatted f-string used in SQL execution.

**Why:** User input from `username` flows directly into the SQL string, allowing
an attacker to inject arbitrary SQL clauses via a crafted username value.

```suggestion
cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
```
```

---

### Skill L: `telemetry-and-cost-optimizer`

```yaml
---
name: telemetry-and-cost-optimizer
description: >
  Tracks end-to-end review execution latency, token usage by model, estimates
  exact USD cost, and manages SHA-256 content-hash memoization to skip redundant
  static analysis re-runs on unchanged diffs.
version: 2.0.0
assigned_to_agent: flow_orchestrator   # PRCodeReviewFlow (continuous)
assigned_tools:
  - TelemetryTracker   # src/code_review_agent/observability/telemetry.py
  - ContentHashCache   # src/code_review_agent/cache.py
task: All flow steps (continuous monitoring)
---
```

#### 📋 Token Cost Pricing Table (as of v2.0.0)

| Model | Prompt (per 1M tokens) | Completion (per 1M tokens) |
| :--- | :--- | :--- |
| `gemini/gemini-3.1-flash-lite-preview` | \$0.0375 | \$0.15 |
| Default fallback | \$0.0375 | \$0.15 |

#### 📋 Cache Key Strategy
- Cache key = `SHA-256(namespace + ":" + raw_diff_content)`
- Cache TTL for SAST results: unlimited (deterministic, no LLM call)
- Cache TTL for Code Graph index: 300 seconds (5 min TTL for repository changes)
- Cache capacity: 2000 entries, LRU eviction

---

## 6. Complete Skill Execution Matrix

| # | Skill ID | Assigned Agent | Task | Execution Layer | Blocking Capable |
| :--- | :--- | :--- | :--- | :--- | :--- |
| A | `senior-dev-quality-reviewer` | `senior_developer` | `analyze_code_quality` | Multi-Agent Crew | ✅ `critical_issues` |
| B | `ast-callgraph-context-indexer` | `senior_developer` | `analyze_code_quality` | Multi-Agent Crew | ⚠️ `cross_file_risks` |
| C | `api-breaking-change-detector` | `senior_developer` | `analyze_code_quality` | Multi-Agent Crew | ✅ Breaking changes → `critical_issues` |
| D | `performance-and-concurrency-auditor` | `senior_developer` | `analyze_code_quality` | Multi-Agent Crew | ✅ Deadlocks → `critical_issues` |
| E | `sast-vulnerability-auditor` | `security_engineer` | `review_security` | Multi-Agent Crew | ✅ `blocking=true` on CRITICAL/HIGH |
| F | `git-diff-and-patch-analyzer` | `security_engineer` | `review_security` | Multi-Agent Crew | ❌ (context reasoning only) |
| G | `tech-lead-verdict-synthesizer` | `tech_lead` | `summarize_findings` | Multi-Agent Crew | ✅ Final `APPROVE`/`REQUEST CHANGES`/`ESCALATE` |
| H | `automated-unit-test-generator` | `tech_lead` | `summarize_findings` | Multi-Agent Crew | ❌ (test scaffolding) |
| I | `governance-policy-enforcer` | `tech_lead` | `summarize_findings` | Multi-Agent Crew | ✅ BLOCKING rule → `ESCALATE` |
| J | `sarif-v21-compliance-exporter` | Flow Orchestrator | `make_final_decision` | Flow Level | ❌ (reporting) |
| K | `github-pr-comment-generator` | Flow Orchestrator | `make_final_decision` | Flow Level | ❌ (integration) |
| L | `telemetry-and-cost-optimizer` | Flow Orchestrator | All Flow Steps | Flow Level | ❌ (observability) |

---

> **Skill Directory Convention:** Each skill folder lives under `skills/{skill-id}/SKILL.md` with optional `scripts/`, `references/`, and `examples/` subdirectories.  
> **Skill Loading:** Agents dynamically read `assigned_skills` from `config/agents.yaml`. The `crew.py` `_build_skill_context()` method injects skill IDs into each agent's `backstory` at crew build time.
