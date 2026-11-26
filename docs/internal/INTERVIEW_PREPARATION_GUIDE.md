# AI Code Review Agent — Interview Preparation and Architecture Deep Dive

## 1. Executive summary

This project is a production-oriented code intelligence platform that reviews pull
requests and source uploads. It combines:

- deterministic static analysis for fast, reproducible evidence;
- repository context from an AST-based symbol and call graph;
- semantic retrieval for code outside the changed diff;
- configurable governance and ticket-compliance checks;
- a CrewAI multi-agent review for architectural and security reasoning;
- generated regression tests whose evidence is reported separately from model
  confidence;
- durable asynchronous jobs, platform adapters, SARIF export, a REST/webhook
  gateway, an MCP server, and a React dashboard.

The strongest one-sentence description is:

> “I built a deterministic-first, evidence-grounded code review pipeline: static
> tools establish facts, repository intelligence supplies context, specialized
> agents reason about quality and security, and a reconciliation stage produces a
> traceable verdict instead of trusting an LLM response blindly.”

The most important architectural principle is **deterministic first, probabilistic
second**. Static analyzers, diff parsing, governance rules, input validation, queue
state, and test-execution outcomes should not be delegated to an LLM. The LLM is
used where contextual reasoning is valuable and deterministic rules are
insufficient.

---

## 2. The problem and the product decision

### Problem

A diff-only AI reviewer is fast to build but has four serious weaknesses:

1. It cannot see callers, implementations, or contracts outside the diff.
2. It may report plausible but untrue vulnerabilities.
3. It has no durable lifecycle for findings across PR revisions.
4. It is difficult to operate safely when webhooks, files, generated tests, and
   third-party model calls are involved.

### Product decision

The project treats review as a pipeline rather than a single prompt:

```text
Input
  -> validate and normalize
  -> durable job / bounded execution
  -> parse changed lines
  -> deterministic SAST and governance
  -> repository graph and semantic context
  -> parallel quality + security agents
  -> technical-lead synthesis
  -> empirical test evidence
  -> deterministic reconciliation
  -> dashboard / inline comments / SARIF / platform status
```

This improves trust and makes the system explainable to engineers, security
teams, and engineering managers.

---

## 3. Architecture at a glance

### Logical layers

| Layer | Responsibility | Main implementation |
|---|---|---|
| Ingestion | Accept PR URLs, diffs, files, ZIPs, and provider webhooks | `webhook_server.py`, `review_service.py` |
| Protection | Authentication/signature checks, rate limits, size/path validation | `gateway_security.py`, `review_service.py` |
| Orchestration | Own review state and stage transitions | `main.py`, `models.py` |
| Deterministic analysis | Parse diffs, scan SAST patterns, evaluate policies | `diff_parser.py`, `tools/`, `governance/` |
| Repository intelligence | Find symbols, callers, impact, and semantically related code | `context_engine/` |
| Probabilistic reasoning | Specialized quality, security, and synthesis agents | `crews/code_review_crew/` |
| Verification | Generate and optionally execute regression tests | `tools/test_generator.py`, `sandbox/` |
| Reconciliation | Deduplicate, normalize severity, calculate verdict and score | `synthesis/reconciler.py` |
| Delivery | Store jobs, publish comments, export SARIF, expose APIs | `webhook_queue.py`, adapters, `sarif_exporter.py` |
| User experience | Submit reviews, observe progress, inspect findings | `frontend/src/` |

### Runtime components

```text
Browser / Git provider / CLI / MCP client
                 |
                 v
          FastAPI gateway
       /       |        \
      /        |         \-- health, jobs, results, commands
     v         v
 validation  SQLite WAL queue
                 |
                 v
          bounded worker loop
                 |
                 v
       isolated review subprocess
                 |
                 v
         PRCodeReviewFlow
                 |
       +---------+----------+
       |                    |
 deterministic stages   CrewAI stages
       |                    |
       +---------+----------+
                 v
        reconciled ReviewState
                 |
     +-----------+------------+
     |           |            |
 dashboard   provider API   SARIF/MCP
```

The web process and review execution are intentionally separated. A slow or
misbehaving review should not block the API process or permanently consume a
worker slot.

---

## 4. End-to-end request walkthrough

### 4.1 Ingestion

The system accepts:

- a GitHub PR URL;
- a raw unified diff;
- a single uploaded source file;
- a ZIP repository snapshot;
- signed GitHub, GitLab, or Bitbucket webhook events.

`ReviewService` normalizes non-diff inputs into unified diff format. ZIP uploads
are bounded by byte count, file count, uncompressed size, and single-file size,
and traversal paths are rejected before extraction.

**Why normalize to a unified diff?**

- It gives every downstream stage one contract.
- Added-line filtering becomes consistent.
- Line mapping and inline comments become possible.
- SAST can avoid reporting unchanged legacy code as a new regression.

**Alternative:** pass raw files to each analyzer. That makes each analyzer
reimplement file discovery, changed-line logic, and path handling, creating
inconsistent results and more security bugs.

### 4.2 Durable asynchronous execution

The gateway stores work in a SQLite WAL-backed queue. Jobs have status, attempts,
retry limits, leases, heartbeats, priority, stage progress, and idempotency keys.
Workers claim jobs with an immediate transaction so two worker processes do not
process the same delivery concurrently.

The browser receives a job ID and polls its status. It stores only that opaque ID
in session storage, so a page refresh can reconnect without retaining source
content in browser state.

**Why SQLite first?**

- It is zero-operations for a single-node deployment.
- WAL and transactions provide durable state and concurrent readers.
- It is easy to run locally and in Docker.

**Trade-off:** SQLite is not the ideal shared queue for many independent
instances, high throughput, or geographically distributed workers. A production
scale-out option would be PostgreSQL plus a queue such as Redis Streams, SQS,
RabbitMQ, or Kafka. The queue contract should remain provider-neutral so this
change does not leak into review logic.

### 4.3 Bounded subprocess execution

`review_executor.py` runs the review worker in a child process with a bounded
semaphore, deadline, temporary request/result files, and process-tree cleanup.
Cancellation and timeout are explicit rather than relying on cooperative cleanup
inside third-party libraries.

This is a **lifecycle boundary**, not by itself a security sandbox. Generated
untrusted tests are separately restricted to a reviewed immutable Docker image,
no network, read-only source mount, dropped capabilities, non-root user, resource
limits, and bounded output.

**Why subprocesses instead of threads?**

- A process can be terminated if a dependency hangs.
- Memory and interpreter state are not shared with the web server.
- Third-party background threads cannot permanently hold the request path.

**Alternative:** an in-process async task is simpler and cheaper, but cancellation
is weaker and a library-level leak can damage the entire service.

### 4.4 Deterministic pre-scan

The flow parses the diff, runs the unified SAST engine, indexes the repository,
and evaluates governance rules before expensive LLM calls.

The SAST layer combines:

- AST security scanning for syntax-aware patterns;
- Semgrep where available;
- Bandit for Python security checks;
- Ruff for quality/lint signals;
- carefully scoped regex heuristics for fast coverage.

Findings carry a rule ID, CWE, category, severity, file, line, snippet,
recommendation, analyzer source, and deterministic fingerprint.

**Why multiple analyzers?**

No single analyzer sees every class of problem:

- regex is cheap but context-blind;
- AST is more precise for language structure;
- Bandit/Ruff reuse mature rules;
- Semgrep provides extensible pattern matching;
- LLM reasoning handles semantic and architectural context.

The key design is not “more tools always equals better.” It is to merge and
reconcile overlapping evidence so one SQL injection is not reported four times.

### 4.5 Smart routing

The flow decides whether a fast path is sufficient or whether the multi-agent
crew is required. Cosmetic changes, low-risk diffs, and no violations can avoid
the most expensive reasoning. Complex logic, security hits, governance
violations, and cross-file impact escalate.

**Why route dynamically?**

- Lower model cost and latency for trivial changes.
- More review depth where the risk justifies it.
- A predictable answer to “why did this PR receive a full review?”

**Alternative:** always run the full crew. That is simpler but wastes tokens,
increases latency, and makes the product harder to scale.

### 4.6 Repository context

The code graph indexes Python with native AST and uses lighter structured
extraction for JavaScript, TypeScript, Go, and Java. Symbols use qualified names
to reduce collisions, and reverse call relationships identify callers affected by
changed functions.

Semantic retrieval complements the graph. It chunks code at useful boundaries,
embeds it with Gemini or an offline hashing embedder, reranks results, and injects
related code outside the diff.

**Why both graph and vector retrieval?**

- A call graph answers structural questions: “who calls this function?”
- Semantic retrieval answers conceptual questions: “where is the same business
  rule or validation pattern implemented?”
- Either one alone misses useful context.

**Known limitation:** Python graph accuracy is stronger than the heuristic
extractors for other languages. A future enhancement should use Tree-sitter or
language-server protocols for consistent multi-language symbol resolution.

### 4.7 Multi-agent reasoning

The crew separates responsibilities:

1. **Senior Developer**: maintainability, architecture, API compatibility,
   performance, cross-file impact.
2. **Security Engineer**: OWASP/CWE analysis, added-line discipline, scanner
   deduplication, secure replacement suggestions.
3. **Tech Lead**: provenance-constrained synthesis, governance reflection,
   confidence arithmetic, verdict, and generated tests.

The quality and security tasks run in parallel; the Tech Lead runs after both
outputs are available.

**Why multi-agent instead of one large prompt?**

- Distinct roles make evaluation and prompt ownership clearer.
- Parallel first-stage tasks reduce wall-clock time.
- A synthesis role can enforce a contract instead of exposing raw, conflicting
  comments to the user.

**Trade-off:** multi-agent systems cost more, are harder to debug, and can create
  false authority if the final agent is not constrained. That is why the project
  uses typed output schemas, guardrails, upstream provenance requirements, and a
  deterministic reconciler.

### 4.8 Verification and reconciliation

The Tech Lead can generate pytest regression tests. If an approved immutable
sandbox is not configured, the system reports `UNVERIFIED` rather than pretending
that a test ran. This distinction is essential:

- `REPRODUCED`: the test demonstrated the defect;
- `PASSING`: the generated test passed, which may indicate the defect was not
  reproduced;
- `UNVERIFIED`: execution was unavailable or refused;
- `HEURISTIC`: evidence came from analysis rather than execution.

The final reconciliation stage deduplicates findings, chooses one severity,
normalizes CWE/category data, applies lifecycle and suppression state, validates
test badges, and computes a bounded score/verdict.

**Why reconcile deterministically after the LLM?**

The LLM is good at proposing explanations but should not be the final authority
for identity, severity normalization, duplicate removal, or whether a test
actually executed.

---

## 5. Important data contracts

### `ReviewState`

The flow state is the central contract. It carries input source, parsed PR,
metadata, SAST findings, code graph context, semantic context, governance
violations, ticket compliance, crew outputs, generated tests, telemetry, errors,
final answer, and optional SARIF path.

### `SastFinding`

This is the normalized deterministic finding contract. The fingerprint is derived
from normalized path, rule ID, and snippet, which supports lifecycle comparison
when line numbers move between commits.

### `ReviewAPIResponse`

The API response is structured for both the dashboard and integrations. It
contains the human-facing summary plus findings, inline comments, impact data,
pipeline/telemetry information, evidence, and machine-readable status fields.

### SARIF

SARIF makes the output consumable by GitHub code scanning and other security
systems. This avoids locking the project to a proprietary dashboard.

---

## 6. “Why this?” interview answers

### Why use an LLM at all?

Static tools are excellent at known patterns but weak at intent, architecture,
business rules, API compatibility, and multi-file reasoning. The LLM is used for
interpretation and prioritization, not for facts that can be computed reliably.

### Why not use only Semgrep/Bandit/Ruff?

That would be fast and deterministic but would miss semantic defects, design
regressions, and repository-wide consequences. The project combines both and
keeps their evidence visible.

### Why CrewAI?

CrewAI provides a convenient flow/agent/task abstraction and supports explicit
task dependencies. It is not a requirement of the architecture: the agents could
later be implemented with direct provider SDKs, LangGraph, or a workflow engine.
The durable contracts are the typed state, task outputs, and reconciliation
rules—not the framework brand.

### Why Gemini and a provider factory?

Gemini offers a practical default for long code context and cost/latency trade-offs.
`LLMFactory` keeps provider selection behind one boundary, making OpenAI,
Anthropic, a self-hosted model, or an offline mode easier to add and test.

### Why a code graph instead of only RAG?

Embeddings find similar meaning but do not guarantee structural relationships.
Callers, callees, qualified symbols, and impacted files are more explainable and
useful for signature/API changes.

### Why a local hashing embedder?

It permits offline development and deterministic tests without API cost. It is
not expected to match a strong semantic model in production; it is a
degraded-mode and testability option.

### Why a durable queue instead of FastAPI `BackgroundTasks`?

`BackgroundTasks` is process-local and does not survive restart. A durable queue
supports retries, recovery of stale leases, idempotency, progress, and a separate
worker lifecycle.

### Why a React dashboard if PR comments are the primary interface?

Inline comments are excellent at the point of change, but a dashboard can show
pipeline progress, cross-file impact, evidence, queue state, telemetry, and an
executive summary. The two interfaces serve different audiences.

### Why support multiple providers?

The review domain is provider-independent. An adapter boundary prevents GitHub
API types from spreading through the orchestration layer and creates a path to
GitLab, Bitbucket, and local/offline workflows.

### Why export SARIF?

SARIF is an industry-standard interchange format. It lets security teams use
existing code scanning, dashboards, and policy tooling instead of adopting a
custom output format.

---

## 7. Security and reliability story

### Threats addressed

- forged provider webhooks: HMAC verification and fail-closed secret handling;
- zip-slip/path traversal: path normalization, resolution, and containment checks;
- denial of service: upload limits, queue capacity, rate limits, bounded output,
  deadlines, and concurrency caps;
- credential leakage: environment scrubbing before sandbox execution;
- malicious generated tests: immutable image, no network, non-root, read-only
  mount, dropped capabilities, resource and process limits;
- duplicate webhook delivery: idempotency keys;
- stale workers: leases, heartbeats, and recovery;
- false evidence: explicit `UNVERIFIED` status when execution did not happen.

### What still needs careful production treatment

1. The default local/demo mode is not a complete multi-tenant authorization
   model. Before public exposure, enable operator authentication and repository
   authorization.
2. Secrets should be managed by a deployment secret manager rather than plain
   environment files.
3. Provider and PR content are untrusted input. Prompt-injection resistance,
   output validation, and tool allowlists must be treated as security controls.
4. A container boundary is only as strong as its runtime configuration and image
   provenance. Image digest pinning and CI probes should be mandatory.
5. Logs and telemetry must avoid source-code and token leakage and should have
   retention/redaction policies.

### How to answer “is the LLM secure?”

Do not claim that an LLM is secure by itself. Say:

> “The model is treated as an untrusted reasoning component. We constrain what it
> sees, require structured output, validate its claims against deterministic
> evidence, and never treat generated test execution as safe without a separate
> sandbox.”

---

## 8. Testing and evaluation strategy

The project has several useful test categories:

- unit tests for parsing, fingerprints, rules, scanners, models, and adapters;
- security tests for gateway authentication, path traversal, ZIP validation, and
  sandbox isolation;
- queue tests for idempotency, leases, retries, cancellation, and concurrency;
- deterministic benchmark tests with known vulnerable and clean diffs;
- optional LLM evaluation tests;
- slow integration tests for subprocess-backed tools;
- frontend build, lint, and Playwright browser tests.

### Metrics to discuss

- precision and recall by vulnerability category;
- false-positive rate per PR and per severity;
- reviewer acceptance rate of suggestions;
- time to first useful finding;
- end-to-end latency and queue wait time;
- token cost per review;
- sandbox execution rate and evidence coverage;
- regression rate across model/provider changes;
- percentage of findings with provenance and actionable fixes.

The README reports benchmark results, but in an interview distinguish:

- benchmark performance on a curated dataset;
- production performance on representative repositories;
- human usefulness, which requires reviewer feedback and accepted/rejected
  suggestion tracking.

### Evaluation improvements

1. Maintain a versioned, balanced dataset with clean examples and hard negatives.
2. Measure precision/recall separately for deterministic and LLM-only findings.
3. Add mutation testing to verify that the scanner catches meaningful changes.
4. Run model/provider drift evaluations in CI.
5. Measure calibration: when the system says high confidence, how often is it
   correct?
6. Track duplicate rate and comment fatigue, not only recall.

---

## 9. Current limitations to acknowledge honestly

These are not reasons to reject the project; they are the answers that show
engineering maturity:

- LLM calls introduce latency, cost, provider outages, and nondeterminism.
- A regex or heuristic SAST match can be wrong; it must be treated as evidence,
  not proof.
- The non-Python code graph is intentionally approximate today.
- Semantic indexes require invalidation and persistence strategy as repositories
  grow.
- SQLite is a strong single-node choice but not a universal distributed queue.
- Generated tests can reproduce a symptom without proving the complete root cause.
- Ticket compliance depends on external ticket availability and parsability.
- Multi-agent prompts and schemas require regression tests as they evolve.
- A review bot should assist human reviewers; it should not silently approve
  security-sensitive code without organization policy.

---

## 10. Improvement roadmap

### Near term

- Add a clear authentication/authorization policy for every read and write route.
- Add repository-scoped tenancy and ownership checks.
- Add explicit prompt-injection tests using adversarial PR content.
- Persist semantic indexes by repository commit and invalidate incrementally.
- Expand provenance so each final finding links to scanner rule, agent output,
  retrieved snippets, and test evidence.
- Add a provider health/circuit-breaker layer and budget limits.

### Medium term

- Replace heuristic multi-language graph parsing with Tree-sitter or LSP-backed
  indexing.
- Move shared deployments to PostgreSQL plus a managed queue.
- Add incremental review mode that analyzes only changed symbols and impacted
  callers.
- Add policy-as-code versioning and approval workflows for suppressions.
- Add organization-level dashboards for precision, latency, cost, and reviewer
  adoption.
- Add staged rollout and shadow mode for new models and prompts.

### Long term

- Build a dependency/service graph across repositories and API contracts.
- Support repository-specific fine-tuning or retrieval adapters only when data
  volume and governance justify it.
- Add automatic patch proposals with human approval and branch isolation.
- Add formal policy controls for merge blocking and risk acceptance.
- Use event-driven distributed workers with autoscaling and per-tenant budgets.

### Prioritization framework

Prioritize work by:

```text
user risk x frequency x blast radius x confidence of improvement
---------------------------------------------------------------
engineering cost
```

For example, repository authorization and prompt-injection defenses should
precede a more sophisticated embedding model because their risk reduction is
larger.

---

## 11. Stakeholder and recruiter narrative

### 30-second version

> “This project is an AI code review platform, but the important part is that it
> is not prompt-only. It parses the diff, runs deterministic security and quality
> checks, understands callers and related repository code, then uses separate
> quality and security agents to reason about what static tools cannot prove. A
> final deterministic stage deduplicates and reconciles the result, and optional
> sandboxed tests provide empirical evidence. It also has a durable queue,
> provider adapters, SARIF export, webhooks, and a React dashboard.”

### Two-minute version

Explain the problem, the deterministic-first decision, the end-to-end flow, one
security boundary, one scalability decision, and one limitation. Finish with:

> “The design lets us improve the model without making the model the system of
> record.”

### Stakeholder value

| Stakeholder | Value |
|---|---|
| Developer | Faster feedback, inline comments, actionable fixes |
| Reviewer | Less routine work and cross-file context |
| Security team | CWE/SARIF evidence, policy enforcement, auditability |
| Engineering manager | Queue visibility, latency/cost telemetry, quality metrics |
| Platform team | Webhooks, retries, health checks, Docker deployment |
| Recruiter/interviewer | Demonstrates systems design, AI grounding, security, and operations |

---

## 12. Questions you should be ready to answer

### Architecture

1. What happens if the LLM provider is unavailable?
2. What happens if a worker crashes halfway through a review?
3. How do you prevent duplicate webhook processing?
4. Which component owns the authoritative verdict?
5. How would you deploy this for 1,000 concurrent repositories?
6. Where would you add caching and what is the cache key?
7. How do you handle a PR larger than the context window?
8. How do you know a finding belongs to the changed code?

### AI and evaluation

1. How do you measure hallucinations?
2. Why is multi-agent better here than one prompt?
3. How do you prevent agents from inventing findings?
4. How do you evaluate prompt/model changes?
5. What is the fallback when embeddings or the provider are unavailable?
6. How do you explain confidence to a user without overstating certainty?

### Security

1. Can generated tests execute arbitrary code?
2. What prevents a malicious ZIP archive from escaping its directory?
3. How are webhook signatures verified?
4. Is the API safe to expose publicly?
5. What data is sent to the model provider?
6. How do you prevent prompt injection from source comments or PR text?

### Product and operations

1. When should a review block a merge?
2. How do suppressions avoid becoming a way to hide vulnerabilities?
3. How do you control token cost?
4. What is the expected latency for a small versus large PR?
5. Which metric would tell you the product is failing?

### Strong answer pattern

For each question, answer in this order:

1. state the current behavior;
2. explain why it was chosen;
3. name the trade-off;
4. describe the alternative;
5. state how you would measure or improve it.

Example:

> “Today the queue uses SQLite WAL because it gives durable, transactional jobs
> with no extra service for a single-node deployment. The trade-off is limited
> distributed throughput. At larger scale I would move the same queue contract to
> PostgreSQL and a managed broker, then measure queue wait time, duplicate rate,
> and recovery behavior before and after.”

---

## 13. Files to show during an interview

Start with the architecture and then show one file from each concern:

1. [`README.md`](../README.md) — product problem and design principles.
2. [`src/code_review_agent/main.py`](../src/code_review_agent/main.py) — flow
   orchestration and routing.
3. [`src/code_review_agent/models.py`](../src/code_review_agent/models.py) —
   typed state and finding contracts.
4. [`src/code_review_agent/tools/sast_scanner.py`](../src/code_review_agent/tools/sast_scanner.py)
   — deterministic-first security evidence.
5. [`src/code_review_agent/context_engine/code_graph.py`](../src/code_review_agent/context_engine/code_graph.py)
   — cross-file context.
6. [`src/code_review_agent/crews/code_review_crew/crew.py`](../src/code_review_agent/crews/code_review_crew/crew.py)
   — role separation and parallelism.
7. [`src/code_review_agent/webhook_queue.py`](../src/code_review_agent/webhook_queue.py)
   — durable jobs and idempotency.
8. [`src/code_review_agent/review_executor.py`](../src/code_review_agent/review_executor.py)
   — bounded subprocess lifecycle.
9. [`src/code_review_agent/sandbox/test_runner.py`](../src/code_review_agent/sandbox/test_runner.py)
   — evidence safety boundary.
10. [`src/code_review_agent/synthesis/reconciler.py`](../src/code_review_agent/synthesis/reconciler.py)
    — deterministic final authority.
11. [`frontend/src/App.tsx`](../frontend/src/App.tsx) — async UX and job recovery.
12. [`tests/`](../tests/) — proof that the architecture is exercised.

---

## 14. Final positioning

The project is compelling because it demonstrates more than model integration:

- systems thinking from ingestion through delivery;
- separation of deterministic and probabilistic responsibilities;
- explicit security boundaries;
- asynchronous reliability and recovery;
- typed contracts and testable components;
- multi-provider and standards-based integration;
- a realistic understanding of limitations.

Do not claim that the system eliminates human review, proves every vulnerability,
or has perfect benchmark accuracy. Claim that it makes review faster, more
context-aware, more evidence-driven, and more operationally reliable while
keeping humans in control of merge policy and risk acceptance.
