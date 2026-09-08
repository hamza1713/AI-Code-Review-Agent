"""
AST-based Security Scanner with Constant Folding and Variable Indirection Tracking.
Analyzes Python AST structures to detect security vulnerabilities that span multiple lines,
variables, or involve constant folding (e.g. 0o777, stat masks, aliased functions, indirect path joins).
"""

import ast
import re
import stat
from typing import List, Dict, Any, Optional, Set, Tuple
from pathlib import Path

from code_review_agent.models import SastFinding
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


# Known standard library constant values for constant folding
_KNOWN_CONSTANTS: Dict[str, Any] = {
    "stat.S_IRWXU": stat.S_IRWXU,  # 0o700 (448)
    "stat.S_IRUSR": stat.S_IRUSR,  # 0o400
    "stat.S_IWUSR": stat.S_IWUSR,  # 0o200
    "stat.S_IXUSR": stat.S_IXUSR,  # 0o100
    "stat.S_IRWXG": stat.S_IRWXG,  # 0o070 (56)
    "stat.S_IRGRP": stat.S_IRGRP,  # 0o040
    "stat.S_IWGRP": stat.S_IWGRP,  # 0o020
    "stat.S_IXGRP": stat.S_IXGRP,  # 0o010
    "stat.S_IRWXO": stat.S_IRWXO,  # 0o007 (7)
    "stat.S_IROTH": stat.S_IROTH,  # 0o004
    "stat.S_IWOTH": stat.S_IWOTH,  # 0o002
    "stat.S_IXOTH": stat.S_IXOTH,  # 0o001
    "ssl.CERT_NONE": 0,
    "ssl.CERT_OPTIONAL": 1,
    "ssl.CERT_REQUIRED": 2,
    "True": True,
    "False": False,
    "None": None,
}


def evaluate_ast_constant(node: ast.AST, env: Dict[str, Any]) -> Optional[Any]:
    """
    Evaluate an AST expression node into a constant Python value using constant folding.
    Handles literals, unary negation, bitwise OR, addition, and variable references.
    """
    if isinstance(node, ast.Constant):
        return node.value

    # Variable lookup
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _KNOWN_CONSTANTS:
            return _KNOWN_CONSTANTS[node.id]
        return None

    # Attribute lookup (e.g. stat.S_IRWXU or ssl.CERT_NONE)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            full_attr = f"{node.value.id}.{node.attr}"
            if full_attr in _KNOWN_CONSTANTS:
                return _KNOWN_CONSTANTS[full_attr]
            if full_attr in env:
                return env[full_attr]
        return None

    # Unary operations (e.g. -1, ~mask)
    if isinstance(node, ast.UnaryOp):
        operand_val = evaluate_ast_constant(node.operand, env)
        if operand_val is not None:
            if isinstance(node.op, ast.USub) and isinstance(operand_val, (int, float)):
                return -operand_val
            if isinstance(node.op, ast.Invert) and isinstance(operand_val, int):
                return ~operand_val
            if isinstance(node.op, ast.Not):
                return not operand_val
        return None

    # Binary operations (e.g. 0o700 | 0o070 | 0o007, or 'prefix_' + 'suffix')
    if isinstance(node, ast.BinOp):
        left_val = evaluate_ast_constant(node.left, env)
        right_val = evaluate_ast_constant(node.right, env)

        if left_val is not None and right_val is not None:
            # Bitwise OR for permission masks
            if isinstance(node.op, ast.BitOr) and isinstance(left_val, int) and isinstance(right_val, int):
                return left_val | right_val
            # Bitwise AND
            if isinstance(node.op, ast.BitAnd) and isinstance(left_val, int) and isinstance(right_val, int):
                return left_val & right_val
            # String concatenation
            if isinstance(node.op, ast.Add) and isinstance(left_val, str) and isinstance(right_val, str):
                return left_val + right_val
            # Numeric addition
            if isinstance(node.op, ast.Add) and isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val + right_val

    return None


class ASTSecurityScanner:
    """
    Performs AST constant folding and cross-statement variable indirection analysis
    on added/modified Python code in pull requests.
    """

    @classmethod
    def scan_diff(cls, raw_diff: str) -> List[SastFinding]:
        """Scan Python files in diff using AST constant folding and variable tracking."""
        if not raw_diff or not raw_diff.strip():
            return []

        parsed_pr = DiffParser.parse_diff(raw_diff)
        py_files = [f for f in parsed_pr.files if f.target_file.endswith(".py") and not f.is_deleted_file]
        if not py_files:
            return []

        findings: List[SastFinding] = []

        for file_diff in py_files:
            file_path = file_diff.target_file
            added_lines_map = dict(DiffParser.extract_added_lines_with_numbers(file_diff))
            if not added_lines_map:
                continue

            # Reconstruct Python file content from patch
            clean_lines = []
            line_no_mapping = {}  # 1-indexed line in clean_lines -> actual target line_no
            curr_target_line = 1

            for raw_line in file_diff.raw_patch.splitlines():
                if raw_line.startswith("@@"):
                    m = re.search(r"\+(\d+)", raw_line)
                    if m:
                        curr_target_line = int(m.group(1))
                    continue
                if raw_line.startswith("---") or raw_line.startswith("+++") or raw_line.startswith("diff "):
                    continue

                if raw_line.startswith("+"):
                    clean_lines.append(raw_line[1:])
                    line_no_mapping[len(clean_lines)] = curr_target_line
                    curr_target_line += 1
                elif raw_line.startswith(" "):
                    clean_lines.append(raw_line[1:])
                    line_no_mapping[len(clean_lines)] = curr_target_line
                    curr_target_line += 1
                elif raw_line.startswith("-"):
                    continue

            code_text = "\n".join(clean_lines)
            if not code_text.strip():
                continue

            try:
                tree = ast.parse(code_text, filename=file_path)
            except SyntaxError:
                # Diff snippet may be an incomplete AST fragment
                continue

            file_findings = cls._analyze_tree(tree, file_path, clean_lines, line_no_mapping, added_lines_map)
            findings.extend(file_findings)

        return findings

    @classmethod
    def _analyze_tree(
        cls,
        tree: ast.AST,
        file_path: str,
        clean_lines: List[str],
        line_no_mapping: Dict[int, int],
        added_lines_map: Dict[int, str],
    ) -> List[SastFinding]:
        findings: List[SastFinding] = []

        # State tables
        env: Dict[str, Any] = dict(_KNOWN_CONSTANTS)
        aliases: Dict[str, str] = {}  # alias_name -> canonical_name
        path_joins: Dict[str, int] = {}  # var_name -> ast_lineno
        sql_concat_vars: Dict[str, int] = {}  # var_name -> ast_lineno
        cmd_concat_vars: Dict[str, int] = {}  # var_name -> ast_lineno

        # Pass 1: Build variable table, aliases, and constant values
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                target_names = []
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        target_names.append(target.id)

                if not target_names:
                    continue

                # 1. Constant folding
                val = evaluate_ast_constant(node.value, env)
                if val is not None:
                    for name in target_names:
                        env[name] = val

                # 2. Function / module aliases (e.g. deser = pickle.loads)
                if isinstance(node.value, ast.Attribute):
                    if isinstance(node.value.value, ast.Name):
                        call_path = f"{node.value.value.id}.{node.value.attr}"
                        for name in target_names:
                            aliases[name] = call_path
                elif isinstance(node.value, ast.Name):
                    if node.value.id in aliases:
                        for name in target_names:
                            aliases[name] = aliases[node.value.id]
                    elif node.value.id in ("eval", "exec"):
                        for name in target_names:
                            aliases[name] = node.value.id

                # 3. Path join assignments: target = os.path.join(...)
                if isinstance(node.value, ast.Call):
                    func_name = cls._get_func_name(node.value.func)
                    if func_name == "os.path.join":
                        for name in target_names:
                            path_joins[name] = node.lineno

                # 4. SQL concatenation tracking: query = 'SELECT... ' + var or base_query + var
                if isinstance(node.value, (ast.BinOp, ast.JoinedStr)) or (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "format"
                ):
                    if cls._is_sql_expression(node.value, env, set(sql_concat_vars.keys())):
                        for name in target_names:
                            sql_concat_vars[name] = node.lineno

                # 5. Command concatenation tracking: cmd = 'ping ' + host
                if isinstance(node.value, (ast.BinOp, ast.JoinedStr)):
                    if cls._is_cmd_expression(node.value, env, set(cmd_concat_vars.keys())):
                        for name in target_names:
                            cmd_concat_vars[name] = node.lineno

            elif isinstance(node, ast.AugAssign):
                if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
                    name = node.target.id
                    if name in sql_concat_vars or cls._is_sql_expression(node.value, env, set(sql_concat_vars.keys())):
                        sql_concat_vars[name] = node.lineno
                    if name in cmd_concat_vars or cls._is_cmd_expression(node.value, env, set(cmd_concat_vars.keys())):
                        cmd_concat_vars[name] = node.lineno

        # Pass 2: Inspect security-sensitive sinks
        for node in ast.walk(tree):
            # ── Sink 1: os.chmod(path, mode) with constant folding ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                if func_name == "os.chmod" and len(node.args) >= 2:
                    mode_arg = node.args[1]
                    folded_mode = evaluate_ast_constant(mode_arg, env)
                    if isinstance(folded_mode, int):
                        if folded_mode in (0o777, 0o666, 511, 438) or (folded_mode & 0o002 != 0 and folded_mode & 0o004 != 0):
                            actual_line = line_no_mapping.get(node.lineno, node.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                                oct_str = oct(folded_mode)
                                findings.append(
                                    SastFinding(
                                        rule_id="SEC-PERM-001",
                                        cwe="CWE-732",
                                        name="Permissive File Permissions (chmod 0777)",
                                        description=(
                                            f"Overly permissive file mask ({oct_str}) grants read/write permissions to all users. "
                                            f"Resolved via AST constant folding on variable/expression."
                                        ),
                                        severity="HIGH",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Restrict permissions to owner only (e.g. 0o600 or 0o700): os.chmod(path, 0o600).",
                                        analyzer_source="ast"
                                    )
                                )

            # ── Sink 2: TLS verification assignment ──
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        attr = target.attr
                        val = evaluate_ast_constant(node.value, env)

                        if attr == "check_hostname" and val is False:
                            actual_line = line_no_mapping.get(node.lineno, node.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                                findings.append(
                                    SastFinding(
                                        rule_id="SEC-TLS-001",
                                        cwe="CWE-295",
                                        name="Disabled TLS Certificate Verification",
                                        description="TLS hostname verification disabled via check_hostname = False (tracked through variable assignment).",
                                        severity="HIGH",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Keep verification enabled: ctx.check_hostname = True and ctx.verify_mode = ssl.CERT_REQUIRED.",
                                        analyzer_source="ast"
                                    )
                                )

                        elif attr == "verify_mode" and val in (0, "ssl.CERT_NONE"):
                            actual_line = line_no_mapping.get(node.lineno, node.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                                findings.append(
                                    SastFinding(
                                        rule_id="SEC-TLS-001",
                                        cwe="CWE-295",
                                        name="Disabled TLS Certificate Verification",
                                        description="TLS certificate verification disabled via verify_mode = ssl.CERT_NONE (tracked through variable indirection).",
                                        severity="HIGH",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Use ssl.CERT_REQUIRED to verify server certificates.",
                                        analyzer_source="ast"
                                    )
                                )

            # ── Sink 3: Path Traversal with indirect variables open(target) ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                if func_name == "open" and node.args:
                    first_arg = node.args[0]
                    if isinstance(first_arg, ast.Name) and first_arg.id in path_joins:
                        actual_line = line_no_mapping.get(node.lineno, node.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-PATH-001",
                                    cwe="CWE-22",
                                    name="Path Traversal / Arbitrary File Read",
                                    description=(
                                        f"Opening file using variable '{first_arg.id}' constructed via unsanitized os.path.join "
                                        f"at line {line_no_mapping.get(path_joins[first_arg.id], path_joins[first_arg.id])}."
                                    ),
                                    severity="HIGH",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Validate path against an allowed base directory using os.path.realpath.",
                                    analyzer_source="ast"
                                )
                            )

            # ── Sink 4: SQL Injection with indirect variables cur.execute(query) ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                is_sql_exec = (
                    func_name.endswith(".execute")
                    or func_name.endswith(".executemany")
                    or func_name.endswith(".query")
                    or func_name in ("execute", "query")
                )
                if is_sql_exec and node.args:
                    first_arg = node.args[0]
                    is_insecure = False
                    if isinstance(first_arg, ast.Name) and first_arg.id in sql_concat_vars:
                        is_insecure = True
                    elif isinstance(first_arg, (ast.BinOp, ast.JoinedStr)) and cls._is_sql_expression(
                        first_arg, env, set(sql_concat_vars.keys())
                    ):
                        is_insecure = True

                    if is_insecure:
                        actual_line = line_no_mapping.get(node.lineno, node.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            arg_desc = first_arg.id if isinstance(first_arg, ast.Name) else "concatenated expression"
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-SQLI-002",
                                    cwe="CWE-89",
                                    name="SQL Query String Concatenation",
                                    description=f"Executing SQL query with dynamic variable/expression '{arg_desc}' constructed via string concatenation.",
                                    severity="CRITICAL",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Use parameterized queries instead of concatenating strings.",
                                    analyzer_source="ast",
                                )
                            )

            # ── Sink 5: Command Injection with indirect variables subprocess.run(cmd, shell=True) ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                is_cmd_exec = func_name in ("subprocess.run", "subprocess.call", "subprocess.Popen", "os.system", "os.popen")
                if is_cmd_exec and node.args:
                    has_shell_true = any(
                        kw.arg == "shell" and evaluate_ast_constant(kw.value, env) is True
                        for kw in node.keywords
                    ) or func_name in ("os.system", "os.popen")

                    first_arg = node.args[0]
                    is_insecure = False
                    if has_shell_true:
                        if isinstance(first_arg, ast.Name) and first_arg.id in cmd_concat_vars:
                            is_insecure = True
                        elif isinstance(first_arg, (ast.BinOp, ast.JoinedStr)) and cls._is_cmd_expression(
                            first_arg, env, set(cmd_concat_vars.keys())
                        ):
                            is_insecure = True

                    if is_insecure:
                        actual_line = line_no_mapping.get(node.lineno, node.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            arg_desc = first_arg.id if isinstance(first_arg, ast.Name) else "concatenated command"
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-CMD-001",
                                    cwe="CWE-78",
                                    name="OS Command Injection / Execution",
                                    description=f"Executing command '{arg_desc}' built via string concatenation with shell=True.",
                                    severity="CRITICAL",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Pass arguments as a list with shell=False.",
                                    analyzer_source="ast",
                                )
                            )

            # ── Sink 6: Aliased calls (e.g. deser(blob), eval_fn(expr)) ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                resolved = aliases.get(func_name, func_name)
                if resolved in ("pickle.loads", "pickle.load"):
                    actual_line = line_no_mapping.get(node.lineno, node.lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                        findings.append(
                            SastFinding(
                                rule_id="SEC-DESER-001",
                                cwe="CWE-502",
                                name="Insecure Deserialization Pattern (Pickle)",
                                description=f"Deserializing untrusted data via aliased function '{func_name}' ({resolved}).",
                                severity="HIGH",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation="Use safe serialization formats like JSON, Protocol Buffers, or MessagePack.",
                                analyzer_source="ast"
                            )
                        )
                elif resolved in ("eval", "exec"):
                    actual_line = line_no_mapping.get(node.lineno, node.lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                        findings.append(
                            SastFinding(
                                rule_id="SEC-EVAL-001",
                                cwe="CWE-95",
                                name="Dynamic Code Evaluation (eval / exec)",
                                description=f"Executing dynamic code via aliased function '{func_name}' ({resolved}).",
                                severity="CRITICAL",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation="Use ast.literal_eval() or avoid dynamic evaluation.",
                                analyzer_source="ast"
                            )
                        )

        return findings

    @staticmethod
    def _get_func_name(node: ast.AST) -> str:
        """Extract full function call name like 'os.chmod' or 'subprocess.run'."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            val = ASTSecurityScanner._get_func_name(node.value)
            return f"{val}.{node.attr}" if val else node.attr
        return ""

    @staticmethod
    def _extract_str_content(node: ast.AST, env: Dict[str, Any]) -> Optional[str]:
        """Extract string content from literal or variable reference."""
        val = evaluate_ast_constant(node, env)
        if isinstance(val, str):
            return val
        return None

    @classmethod
    def _is_sql_expression(cls, node: ast.AST, env: Dict[str, Any], sql_vars: set) -> bool:
        """Check if an expression tree involves SQL keywords (SELECT, INSERT, etc.)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return bool(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", node.value, re.I))
        if isinstance(node, ast.Name):
            if node.id in sql_vars:
                return True
            if node.id in env and isinstance(env[node.id], str):
                return bool(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", env[node.id], re.I))
            return False
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, (ast.Add, ast.Mod)):
                return cls._is_sql_expression(node.left, env, sql_vars) or cls._is_sql_expression(
                    node.right, env, sql_vars
                )
        if isinstance(node, ast.JoinedStr):
            return any(cls._is_sql_expression(val, env, sql_vars) for val in node.values)
        if isinstance(node, ast.FormattedValue):
            return cls._is_sql_expression(node.value, env, sql_vars)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                return cls._is_sql_expression(node.func.value, env, sql_vars)
        return False

    @classmethod
    def _is_cmd_expression(cls, node: ast.AST, env: Dict[str, Any], cmd_vars: set) -> bool:
        """Check if an expression tree involves command execution string concatenation."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return bool(re.search(r"\b(ping|sh|bash|rm|curl|wget|cat|chmod|chown)\b", node.value, re.I))
        if isinstance(node, ast.Name):
            if node.id in cmd_vars:
                return True
            if node.id in env and isinstance(env[node.id], str):
                return bool(re.search(r"\b(ping|sh|bash|rm|curl|wget|cat|chmod|chown)\b", env[node.id], re.I))
            return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return cls._is_cmd_expression(node.left, env, cmd_vars) or cls._is_cmd_expression(
                node.right, env, cmd_vars
            )
        if isinstance(node, ast.JoinedStr):
            return any(cls._is_cmd_expression(val, env, cmd_vars) for val in node.values)
        return False
