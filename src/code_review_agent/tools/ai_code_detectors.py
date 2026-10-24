"""
AI Code Defect Scanner.
Detects common semantic failure modes in AI-generated code:
- AI-ASYNC-001: Unawaited coroutine in condition check (if async_func():).
- AI-ASYNC-002: Blocking synchronous I/O inside async def (time.sleep, requests.get).
- AI-STUB-001: Hollow security/auth stubs (pass, return True, return None).
- AI-CONTRACT-001: Cross-file caller/callee signature and parameter mismatch.
"""

import ast
import re
import textwrap
from typing import Any

from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import SastFinding

_SECURITY_STUB_KEYWORDS = {
    "auth",
    "login",
    "permission",
    "validate",
    "sanitize",
    "check_access",
    "authorize",
    "verify",
    "payment",
    "crypto",
    "token",
    "role",
    "admin",
    "tenant",
    "rate_limit",
}

_BLOCKING_SYNC_CALLS = {
    "time.sleep": "await asyncio.sleep(...) instead of time.sleep in async function",
    "requests.get": "httpx.AsyncClient().get or aiohttp instead of requests.get in async function",
    "requests.post": "httpx.AsyncClient().post or aiohttp instead of requests.post in async function",
    "requests.put": "httpx.AsyncClient().put or aiohttp instead of requests.put in async function",
    "requests.delete": "httpx.AsyncClient().delete or aiohttp instead of requests.delete in async function",
    "urllib.request.urlopen": "async http client instead of urllib.request.urlopen in async function",
    "os.system": "asyncio.create_subprocess_shell instead of os.system in async function",
}


class AICodeDefectScanner:
    """
    AST scanner for AI-generated code antipatterns and semantic flaws.
    """

    @classmethod
    def is_available(cls) -> bool:
        return True

    @classmethod
    def scan_diff(
        cls,
        raw_diff: str,
        code_graph: CodeGraphIndexer | None = None,
    ) -> list[SastFinding]:
        """Scan diff for AI code generation failure modes."""
        if not raw_diff or not raw_diff.strip():
            return []

        findings: list[SastFinding] = []
        parsed = DiffParser.parse_diff(raw_diff)

        for file_diff in parsed.files:
            file_path = (file_diff.target_file or "").replace("\\", "/")
            if file_diff.is_deleted_file or not file_path.endswith(".py"):
                continue

            added_lines_with_numbers = (
                DiffParser.extract_added_lines_with_numbers(file_diff)
            )
            if not added_lines_with_numbers:
                continue

            added_line_numbers = {ln for ln, _ in added_lines_with_numbers}
            snippet_by_line = {ln: text for ln, text in added_lines_with_numbers}

            # Build post-image code blocks from each hunk with line mappings
            blocks_to_scan: list[tuple[ast.AST, dict[int, int]]] = []

            for hunk in file_diff.hunks:
                hunk_lines: list[str] = []
                line_map: dict[int, int] = {}
                curr_target_line = hunk.new_start
                for raw_line in hunk.lines:
                    if raw_line.startswith("-"):
                        continue
                    elif raw_line.startswith("+"):
                        line_map[len(hunk_lines) + 1] = curr_target_line
                        hunk_lines.append(raw_line[1:])
                        curr_target_line += 1
                    else:
                        line_map[len(hunk_lines) + 1] = curr_target_line
                        hunk_lines.append(
                            raw_line[1:]
                            if raw_line.startswith(" ")
                            else raw_line
                        )
                        curr_target_line += 1

                hunk_code = "\n".join(hunk_lines)
                for candidate in (
                    hunk_code,
                    textwrap.dedent(hunk_code),
                    f"async def _wrapper():\n{textwrap.indent(textwrap.dedent(hunk_code), '    ')}",
                ):
                    try:
                        parsed_tree = ast.parse(candidate)
                        blocks_to_scan.append((parsed_tree, line_map))
                        break
                    except Exception:
                        continue

            if not blocks_to_scan:
                # Fallback to regex checks on lines
                cls._scan_lines_regex(
                    file_path, added_lines_with_numbers, findings
                )
                continue

            for tree, line_map in blocks_to_scan:
                known_async_funcs: set[str] = set()

                # First pass: collect local async functions
                for node in ast.walk(tree):
                    if isinstance(node, ast.AsyncFunctionDef):
                        known_async_funcs.add(node.name)

                # Second pass: detect AI-specific defects
                for node in ast.walk(tree):
                    # ── AI-ASYNC-001: Unawaited coroutine in conditions ──
                    if isinstance(node, (ast.If, ast.While, ast.Assert)):
                        cls._check_unawaited_coro(
                            node.test,
                            file_path,
                            added_line_numbers,
                            line_map,
                            snippet_by_line,
                            known_async_funcs,
                            findings,
                        )

                    # ── AI-ASYNC-002: Blocking I/O in async def ──
                    if isinstance(node, ast.AsyncFunctionDef):
                        cls._check_blocking_calls_in_async(
                            node,
                            file_path,
                            added_line_numbers,
                            line_map,
                            snippet_by_line,
                            findings,
                        )

                    # ── AI-STUB-001: Hollow security/auth stubs ──
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        cls._check_hollow_stubs(
                            node,
                            file_path,
                            added_line_numbers,
                            line_map,
                            snippet_by_line,
                            findings,
                        )

                    # ── AI-CONTRACT-001: Cross-file parameter count drift ──
                    if isinstance(node, ast.Call) and code_graph is not None:
                        cls._check_contract_drift(
                            node,
                            file_path,
                            added_line_numbers,
                            line_map,
                            snippet_by_line,
                            code_graph,
                            findings,
                        )

        return findings

    @classmethod
    def _check_unawaited_coro(
        cls,
        test_expr: ast.AST,
        file_path: str,
        added_lines: set[int],
        line_map: dict[int, int],
        snippet_by_line: dict[int, str],
        known_async_funcs: set[str],
        findings: list[SastFinding],
    ):
        """Check if condition expression calls an async function without await."""
        for sub_node in ast.walk(test_expr):
            if isinstance(sub_node, ast.Call):
                func_name = None
                if isinstance(sub_node.func, ast.Name):
                    func_name = sub_node.func.id
                elif isinstance(sub_node.func, ast.Attribute):
                    func_name = sub_node.func.attr

                is_async = False
                if func_name:
                    if func_name in known_async_funcs:
                        is_async = True
                    elif (
                        func_name.startswith("async_")
                        or func_name.endswith("_async")
                        or "is_authorized" in func_name
                        or "check_access" in func_name
                    ):
                        is_async = True

                if is_async:
                    rel_line = getattr(sub_node, "lineno", 1)
                    line_no = line_map.get(rel_line, rel_line)
                    if line_no in added_lines or not added_lines:
                        findings.append(
                            SastFinding(
                                rule_id="AI-ASYNC-001",
                                cwe="CWE-1076",
                                category="SECURITY",
                                name="Unawaited Coroutine in Conditional Check",
                                description=(
                                    f"The asynchronous function '{func_name}()' is invoked inside a condition check without 'await'. "
                                    "In Python, unawaited coroutines evaluate to True regardless of returned values, "
                                    "bypassing the intended security or validation logic."
                                ),
                                severity="HIGH",
                                file_path=file_path,
                                line_number=line_no,
                                snippet=snippet_by_line.get(
                                    line_no, f"if {func_name}(...):"
                                ),
                                fix_recommendation=(
                                    f"Add 'await' to execute the coroutine: 'if await {func_name}(...):'"
                                ),
                                analyzer_source="ai_code_detectors",
                                evidence_grade="HEURISTIC",
                                evidence_provenance={
                                    "rule": "unawaited_async_in_condition",
                                    "callee": func_name,
                                },
                            )
                        )

    @classmethod
    def _check_blocking_calls_in_async(
        cls,
        func_node: ast.AsyncFunctionDef,
        file_path: str,
        added_lines: set[int],
        line_map: dict[int, int],
        snippet_by_line: dict[int, str],
        findings: list[SastFinding],
    ):
        """Detect blocking synchronous calls inside an async function."""
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                call_name = None
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        call_name = f"{node.func.value.id}.{node.func.attr}"
                    elif isinstance(node.func.value, ast.Attribute):
                        if isinstance(node.func.value.value, ast.Name):
                            call_name = f"{node.func.value.value.id}.{node.func.value.attr}.{node.func.attr}"
                elif isinstance(node.func, ast.Name):
                    call_name = node.func.id

                if call_name and call_name in _BLOCKING_SYNC_CALLS:
                    rel_line = getattr(node, "lineno", 1)
                    line_no = line_map.get(rel_line, rel_line)
                    if line_no in added_lines or not added_lines:
                        remediation = _BLOCKING_SYNC_CALLS[call_name]
                        findings.append(
                            SastFinding(
                                rule_id="AI-ASYNC-002",
                                cwe="CWE-834",
                                category="QUALITY",
                                name="Blocking Synchronous Call in Async Context",
                                description=(
                                    f"Calling '{call_name}()' synchronously inside async function '{func_node.name}()' "
                                    "blocks the entire asyncio event loop, starving concurrent requests."
                                ),
                                severity="MEDIUM",
                                file_path=file_path,
                                line_number=line_no,
                                snippet=snippet_by_line.get(
                                    line_no, f"{call_name}(...)"
                                ),
                                fix_recommendation=f"Use {remediation}.",
                                analyzer_source="ai_code_detectors",
                                evidence_grade="HEURISTIC",
                                evidence_provenance={
                                    "rule": "blocking_sync_in_async",
                                    "call": call_name,
                                },
                            )
                        )

    @classmethod
    def _check_hollow_stubs(
        cls,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: str,
        added_lines: set[int],
        line_map: dict[int, int],
        snippet_by_line: dict[int, str],
        findings: list[SastFinding],
    ):
        """Detect hollow stubs (pass, return True/None, ...) in security/business functions."""
        body = [stmt for stmt in node.body if not isinstance(stmt, ast.Expr)]
        if not body:
            is_stub = True
        elif len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, ast.Pass):
                is_stub = True
            elif isinstance(stmt, ast.Return):
                if stmt.value is None or (
                    isinstance(stmt.value, ast.Constant)
                    and stmt.value.value in (True, False, None)
                ):
                    is_stub = True
                else:
                    is_stub = False
            else:
                is_stub = False
        else:
            is_stub = False

        if is_stub:
            fn_name_lower = node.name.lower()
            fp_lower = file_path.lower()
            is_security = any(
                kw in fn_name_lower or kw in fp_lower
                for kw in _SECURITY_STUB_KEYWORDS
            )

            if is_security:
                rel_line = getattr(node, "lineno", 1)
                line_no = line_map.get(rel_line, rel_line)
                if line_no in added_lines or not added_lines:
                    findings.append(
                        SastFinding(
                            rule_id="AI-STUB-001",
                            cwe="CWE-1164",
                            category="SECURITY",
                            name="Hollow Security Function Stub",
                            description=(
                                f"The security-critical function '{node.name}()' is an incomplete or hollow stub "
                                "(contains only pass or default constant return). AI code generators often leave "
                                "these stubs incomplete, causing unintended authorization or validation bypasses."
                            ),
                            severity="HIGH",
                            file_path=file_path,
                            line_number=line_no,
                            snippet=snippet_by_line.get(
                                line_no, f"def {node.name}(...): pass"
                            ),
                            fix_recommendation=(
                                f"Implement complete validation, authentication, or permission logic in '{node.name}()'."
                            ),
                            analyzer_source="ai_code_detectors",
                            evidence_grade="HEURISTIC",
                            evidence_provenance={
                                "rule": "hollow_security_stub",
                                "function": node.name,
                            },
                        )
                    )

    @classmethod
    def _check_contract_drift(
        cls,
        call_node: ast.Call,
        file_path: str,
        added_lines: set[int],
        line_map: dict[int, int],
        snippet_by_line: dict[int, str],
        code_graph: CodeGraphIndexer,
        findings: list[SastFinding],
    ):
        """Check for argument count mismatch against target declaration in CodeGraph."""
        target_name = None
        if isinstance(call_node.func, ast.Name):
            target_name = call_node.func.id
        elif isinstance(call_node.func, ast.Attribute):
            target_name = call_node.func.attr

        if not target_name:
            return

        target_sym = code_graph.get_symbol_definition(target_name)
        if not target_sym or target_sym.file_path == file_path:
            return

        if target_sym.parameters:
            expected_params = [
                p
                for p in target_sym.parameters
                if p not in ("self", "cls", "*args", "**kwargs")
            ]
            num_args = len(call_node.args)
            if num_args > len(expected_params) and not any(
                "*" in p for p in target_sym.parameters
            ):
                rel_line = getattr(call_node, "lineno", 1)
                line_no = line_map.get(rel_line, rel_line)
                if line_no in added_lines or not added_lines:
                    findings.append(
                        SastFinding(
                            rule_id="AI-CONTRACT-001",
                            cwe="CWE-1007",
                            category="QUALITY",
                            name="Cross-File Call Argument Count Mismatch",
                            description=(
                                f"Function '{target_name}()' declared in '{target_sym.file_path}' takes "
                                f"{len(expected_params)} parameter(s) ({', '.join(expected_params)}), "
                                f"but is invoked with {num_args} positional argument(s)."
                            ),
                            severity="HIGH",
                            file_path=file_path,
                            line_number=line_no,
                            snippet=snippet_by_line.get(
                                line_no, f"{target_name}(...)"
                            ),
                            fix_recommendation=(
                                f"Update the call to match the parameter signature of '{target_name}': {', '.join(expected_params)}."
                            ),
                            analyzer_source="ai_code_detectors",
                            evidence_grade="HEURISTIC",
                            evidence_provenance={
                                "rule": "contract_drift_arg_count",
                                "callee": target_name,
                                "expected_count": len(expected_params),
                                "provided_count": num_args,
                            },
                        )
                    )

    @classmethod
    def _scan_lines_regex(
        cls,
        file_path: str,
        added_lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        """Fallback regex scanning for unparsed diff hunks."""
        for line_no, text in added_lines:
            if re.search(r"\bif\s+async_[A-Za-z0-9_]+\s*\(", text):
                findings.append(
                    SastFinding(
                        rule_id="AI-ASYNC-001",
                        cwe="CWE-1076",
                        category="SECURITY",
                        name="Unawaited Coroutine in Conditional Check",
                        description="Potential unawaited async function call in if condition.",
                        severity="HIGH",
                        file_path=file_path,
                        line_number=line_no,
                        snippet=text,
                        fix_recommendation="Ensure the coroutine is awaited.",
                        analyzer_source="ai_code_detectors",
                    )
                )
