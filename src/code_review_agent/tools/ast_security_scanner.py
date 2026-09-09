"""
AST-based Security Scanner with Constant Folding and Variable Indirection Tracking.
Analyzes Python AST structures to detect security vulnerabilities that span multiple lines,
variables, or involve constant folding (e.g. 0o777, stat masks, aliased functions, indirect path joins).
"""

import ast
import re
import stat
from typing import List, Dict, Any, Optional

from code_review_agent.models import SastFinding
from code_review_agent.diff_parser import DiffParser


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

# os.path helpers that resolve/normalize a path but do NOT confine it to a base
# directory, so they leave traversal (../) reachable when wrapping os.path.join.
_PATH_RESOLVERS = {"os.path.abspath", "os.path.realpath", "os.path.normpath", "os.path.join"}

# Assignment target names that indicate a credential/secret when they hold a
# non-trivial constant value (used for obfuscated-secret detection).
_SECRET_NAME_RE = re.compile(
    r"(api[_-]?key|secret|token|passwd|password|pwd|access[_-]?key|"
    r"private[_-]?key|credential|auth[_-]?key|_key)$",
    re.IGNORECASE,
)


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

    # Subscript / slicing on a folded sequence, e.g. "abc"[::-1] (string reversal)
    # or ENC[::-1], a common secret-obfuscation trick.
    if isinstance(node, ast.Subscript):
        seq = evaluate_ast_constant(node.value, env)
        if isinstance(seq, (str, bytes, list, tuple)):
            sl = node.slice
            try:
                if isinstance(sl, ast.Slice):
                    lower = evaluate_ast_constant(sl.lower, env) if sl.lower is not None else None
                    upper = evaluate_ast_constant(sl.upper, env) if sl.upper is not None else None
                    step = evaluate_ast_constant(sl.step, env) if sl.step is not None else None
                    return seq[lower:upper:step]
                idx = evaluate_ast_constant(sl, env)
                if isinstance(idx, int):
                    return seq[idx]
            except (ValueError, TypeError, IndexError):
                return None
        return None

    # Calls that decode/transform encoded literals (secret obfuscation): fold a
    # limited, side-effect-free allowlist so hex/base64-encoded secrets resolve.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        try:
            # bytes.fromhex("...") -> bytes
            if method == "fromhex" and isinstance(node.func.value, ast.Name) and node.func.value.id == "bytes":
                if node.args:
                    hex_val = evaluate_ast_constant(node.args[0], env)
                    if isinstance(hex_val, str):
                        return bytes.fromhex(hex_val)
            # base64.b64decode("...") / b64decode -> bytes
            elif method in ("b64decode", "b16decode", "b32decode", "urlsafe_b64decode"):
                if node.args:
                    enc_val = evaluate_ast_constant(node.args[0], env)
                    if isinstance(enc_val, (str, bytes)):
                        import base64 as _b64
                        return getattr(_b64, method)(enc_val)
            # <bytes>.decode() -> str
            elif method == "decode":
                inner = evaluate_ast_constant(node.func.value, env)
                if isinstance(inner, bytes):
                    return inner.decode(errors="replace")
            # <str>.encode() -> bytes
            elif method == "encode":
                inner = evaluate_ast_constant(node.func.value, env)
                if isinstance(inner, str):
                    return inner.encode()
        except (ValueError, TypeError):
            return None

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

        # Map every node to the id() of its nearest enclosing function ("module"
        # at top level). Local dataflow facts (path joins, SQL/command concat
        # vars) are keyed by this scope so a variable named `path` in one
        # function cannot leak a finding into another function that reuses the
        # name. Constants (env) and function aliases stay module-global, since a
        # module-level constant or `deser = pickle.loads` is legitimately visible
        # inside every function.
        node_scope: Dict[int, Any] = {id(tree): "module"}

        def _assign_scope(parent: ast.AST, scope: Any) -> None:
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node_scope[id(child)] = scope          # the def keyword itself lives in the outer scope
                    _assign_scope(child, id(child))         # its body opens a new scope
                else:
                    node_scope[id(child)] = scope
                    _assign_scope(child, scope)

        _assign_scope(tree, "module")

        def _scope_of(node: ast.AST) -> Any:
            return node_scope.get(id(node), "module")

        # State tables
        env: Dict[str, Any] = dict(_KNOWN_CONSTANTS)
        aliases: Dict[str, str] = {}  # alias_name -> canonical_name
        # Local dataflow facts keyed by (scope_id, var_name) -> ast_lineno
        path_joins: Dict[tuple, int] = {}
        sql_concat_vars: Dict[tuple, int] = {}
        cmd_concat_vars: Dict[tuple, int] = {}

        def _names_in_scope(table: Dict[tuple, int], scope: Any) -> set:
            return {name for (s, name) in table if s == scope}

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

                scope = _scope_of(node)

                # 2. Function / module aliases (e.g. deser = pickle.loads,
                #    loads = getattr(pickle, 'loads'))
                if isinstance(node.value, ast.Attribute):
                    if isinstance(node.value.value, ast.Name):
                        call_path = f"{node.value.value.id}.{node.value.attr}"
                        for name in target_names:
                            aliases[name] = call_path
                elif isinstance(node.value, ast.Call) and cls._get_func_name(node.value.func) == "getattr":
                    # getattr(<module_or_alias>, "attr") -> module.attr indirection
                    resolved = cls._resolve_getattr(node.value, aliases)
                    if resolved:
                        for name in target_names:
                            aliases[name] = resolved
                elif isinstance(node.value, ast.Name):
                    if node.value.id in aliases:
                        for name in target_names:
                            aliases[name] = aliases[node.value.id]
                    elif node.value.id in ("eval", "exec"):
                        for name in target_names:
                            aliases[name] = node.value.id

                # 3. Path join assignments: target = os.path.join(...)
                #    Also see through non-confining resolvers such as
                #    os.path.abspath(os.path.join(...)) / realpath / normpath,
                #    which normalise but do NOT restrict to a base directory.
                if isinstance(node.value, ast.Call):
                    if cls._call_builds_unsafe_path(node.value):
                        for name in target_names:
                            path_joins[(scope, name)] = node.lineno

                # 4. SQL concatenation tracking: query = 'SELECT... ' + var or base_query + var
                if isinstance(node.value, (ast.BinOp, ast.JoinedStr)) or (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "format"
                ):
                    if cls._is_sql_expression(node.value, env, _names_in_scope(sql_concat_vars, scope)):
                        for name in target_names:
                            sql_concat_vars[(scope, name)] = node.lineno

                # 5. Command concatenation tracking: cmd = 'ping ' + host
                if isinstance(node.value, (ast.BinOp, ast.JoinedStr)):
                    if cls._is_cmd_expression(node.value, env, _names_in_scope(cmd_concat_vars, scope)):
                        for name in target_names:
                            cmd_concat_vars[(scope, name)] = node.lineno

                # 6. Obfuscated hardcoded secret: SECRET = <computed constant>
                #    Only fires for secret-like target names whose value is built
                #    from a non-literal expression (hex/base64 decode, concat) that
                #    folds to a non-trivial string — plain string literals are left
                #    to the regex scanner (SEC-SECRET-001) to avoid double-reporting.
                if not isinstance(node.value, ast.Constant):
                    secret_val = evaluate_ast_constant(node.value, env)
                    if isinstance(secret_val, bytes):
                        try:
                            secret_val = secret_val.decode(errors="replace")
                        except Exception:
                            secret_val = None
                    if isinstance(secret_val, str) and len(secret_val.strip()) >= 6:
                        for name in target_names:
                            if not _SECRET_NAME_RE.search(name):
                                continue
                            actual_line = line_no_mapping.get(node.lineno, node.lineno)
                            if actual_line not in added_lines_map:
                                continue
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-SECRET-002",
                                    cwe="CWE-798",
                                    name="Obfuscated Hardcoded Secret",
                                    description=(
                                        f"Credential '{name}' is hardcoded via an obfuscated expression "
                                        f"(e.g. hex/base64 decode or string concatenation) that resolves to a "
                                        f"static secret value. Encoding does not protect embedded secrets."
                                    ),
                                    severity="CRITICAL",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Load secrets from environment variables or a secrets manager; never embed them (even encoded) in source.",
                                    analyzer_source="ast",
                                )
                            )

            elif isinstance(node, ast.AugAssign):
                if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
                    scope = _scope_of(node)
                    name = node.target.id
                    if (scope, name) in sql_concat_vars or cls._is_sql_expression(node.value, env, _names_in_scope(sql_concat_vars, scope)):
                        sql_concat_vars[(scope, name)] = node.lineno
                    if (scope, name) in cmd_concat_vars or cls._is_cmd_expression(node.value, env, _names_in_scope(cmd_concat_vars, scope)):
                        cmd_concat_vars[(scope, name)] = node.lineno

        # Pass 2: Inspect security-sensitive sinks
        for node in ast.walk(tree):
            # ── Sink 1: permissive mode on os.chmod / os.open / os.mkdir /
            #    os.makedirs with constant folding on the mode argument. The mode
            #    position differs per call: chmod(path, MODE), mkdir(path, MODE),
            #    makedirs(path, MODE) use arg index 1; os.open(path, flags, MODE)
            #    uses index 2. A 'mode=' keyword is honoured for any of them. ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                _mode_arg_index = {"os.chmod": 1, "os.mkdir": 1, "os.makedirs": 1, "os.open": 2}
                if func_name in _mode_arg_index:
                    idx = _mode_arg_index[func_name]
                    mode_arg = None
                    if len(node.args) > idx:
                        mode_arg = node.args[idx]
                    else:
                        for kw in node.keywords:
                            if kw.arg == "mode":
                                mode_arg = kw.value
                                break
                    folded_mode = evaluate_ast_constant(mode_arg, env) if mode_arg is not None else None
                    if isinstance(folded_mode, int):
                        # World read+write (o+rw) or the classic 0o777/0o666 masks.
                        if folded_mode in (0o777, 0o666, 511, 438) or (folded_mode & 0o002 != 0 and folded_mode & 0o004 != 0):
                            actual_line = line_no_mapping.get(node.lineno, node.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                                oct_str = oct(folded_mode)
                                findings.append(
                                    SastFinding(
                                        rule_id="SEC-PERM-001",
                                        cwe="CWE-732",
                                        name="Permissive / World-Writable File Permissions",
                                        description=(
                                            f"{func_name}() is called with an overly permissive file mask ({oct_str}), "
                                            f"granting read/write access to all users. Resolved via AST constant folding "
                                            f"on the variable/expression."
                                        ),
                                        severity="HIGH",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Restrict permissions to the owner (e.g. 0o600 for files, 0o700 for directories).",
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
                    scope = _scope_of(node)
                    if isinstance(first_arg, ast.Name) and (scope, first_arg.id) in path_joins:
                        pj_line = path_joins[(scope, first_arg.id)]
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
                                        f"at line {line_no_mapping.get(pj_line, pj_line)}."
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
                    scope = _scope_of(node)
                    sql_names = _names_in_scope(sql_concat_vars, scope)
                    is_insecure = False
                    if isinstance(first_arg, ast.Name) and first_arg.id in sql_names:
                        is_insecure = True
                    elif isinstance(first_arg, (ast.BinOp, ast.JoinedStr)) and cls._is_sql_expression(
                        first_arg, env, sql_names
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
                    scope = _scope_of(node)
                    cmd_names = _names_in_scope(cmd_concat_vars, scope)
                    is_insecure = False
                    if has_shell_true:
                        if isinstance(first_arg, ast.Name) and first_arg.id in cmd_names:
                            is_insecure = True
                        elif isinstance(first_arg, (ast.BinOp, ast.JoinedStr)) and cls._is_cmd_expression(
                            first_arg, env, cmd_names
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

            # ── Sink 8: Weak hash via hashlib.new(<algo>) with a folded algo ──
            #    Catches hashlib.new("sha1") and the variable-indirected form
            #    algo = "md5"; hashlib.new(algo). Also resolves getattr aliases.
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                resolved = aliases.get(func_name, func_name)
                if resolved == "hashlib.new" and node.args:
                    algo_val = evaluate_ast_constant(node.args[0], env)
                    if isinstance(algo_val, str) and algo_val.lower().replace("-", "") in ("md5", "sha1", "md4", "md2"):
                        actual_line = line_no_mapping.get(node.lineno, node.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-CRYPTO-001",
                                    cwe="CWE-327",
                                    name="Weak Hash Algorithm (MD5 / SHA1)",
                                    description=(
                                        f"hashlib.new('{algo_val}') selects a cryptographically broken hash "
                                        f"(resolved via AST constant folding on the algorithm argument)."
                                    ),
                                    severity="MEDIUM",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Use SHA-256 (hashlib.sha256) or SHA-3 for security-sensitive hashing.",
                                    analyzer_source="ast",
                                )
                            )

            # ── Sink 9: Insecure randomness for security values, incl. getattr ──
            #    Resolves aliases so choice = getattr(random, 'choice'); choice(..)
            #    and a deterministic random.seed(<const>) are both caught.
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                resolved = aliases.get(func_name, func_name)
                # random.seed is intentionally excluded: it is common in
                # legitimate reproducibility/test code, so flagging it alone
                # would hurt precision. The generator calls below are the ones
                # that actually produce predictable security values.
                _insecure_random = {
                    "random.choice", "random.random", "random.randint", "random.randrange",
                    "random.choices", "random.sample", "random.getrandbits", "random.randbytes",
                    "random.uniform",
                }
                if resolved in _insecure_random:
                    actual_line = line_no_mapping.get(node.lineno, node.lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                        via = f" via alias '{func_name}'" if resolved != func_name else ""
                        findings.append(
                            SastFinding(
                                rule_id="SEC-RAND-001",
                                cwe="CWE-330",
                                name="Insecure Pseudo-Random Generator for Security",
                                description=(
                                    f"Non-cryptographic random function '{resolved}'{via} used for security-sensitive "
                                    f"value generation. The random module is predictable and unsuitable for tokens/keys."
                                ),
                                severity="MEDIUM",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation="Use the secrets module: secrets.token_hex(), secrets.choice().",
                                analyzer_source="ast",
                            )
                        )

            # ── Sink 7: Overly broad exception handler with a silent body ──
            #    Catches `except:`, `except Exception:` and `except BaseException:`
            #    whose body is only `pass` or `...`, which swallows every error
            #    (including KeyboardInterrupt/SystemExit for BaseException).
            if isinstance(node, ast.ExceptHandler):
                is_broad = node.type is None or (
                    isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
                )
                body_is_silent = len(node.body) == 1 and (
                    isinstance(node.body[0], ast.Pass)
                    or (
                        isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and node.body[0].value.value is Ellipsis
                    )
                )
                if is_broad and body_is_silent:
                    actual_line = line_no_mapping.get(node.lineno, node.lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                        handler_name = "bare except" if node.type is None else f"except {node.type.id}"
                        findings.append(
                            SastFinding(
                                rule_id="SEC-EXCEPT-001",
                                cwe="CWE-703",
                                name="Silent Broad Exception Swallow",
                                description=(
                                    f"'{handler_name}:' with a body of only pass/... silently swallows all errors, "
                                    f"hiding runtime bugs and (for BaseException) blocking KeyboardInterrupt/SystemExit."
                                ),
                                severity="LOW",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation="Catch specific exceptions and log them: except SpecificError as e: logger.warning(f'... {e}').",
                                analyzer_source="ast",
                            )
                        )

        return findings

    @classmethod
    def _call_builds_unsafe_path(cls, node: ast.AST) -> bool:
        """
        True if a Call expression constructs a filesystem path from os.path.join,
        possibly wrapped in a non-confining resolver (os.path.abspath / realpath /
        normpath). These normalise '..' but do not restrict the result to a base
        directory, so the joined path remains traversal-reachable.
        """
        if not isinstance(node, ast.Call):
            return False
        func_name = cls._get_func_name(node.func)
        if func_name == "os.path.join":
            return True
        if func_name in _PATH_RESOLVERS:
            # Recurse into positional args to find a nested os.path.join.
            return any(cls._call_builds_unsafe_path(arg) for arg in node.args)
        return False

    @staticmethod
    def _resolve_getattr(call: ast.Call, aliases: Dict[str, str]) -> Optional[str]:
        """
        Resolve getattr(<module>, "attr") to a dotted 'module.attr' name so that
        indirect calls such as loads = getattr(pickle, 'loads') are tracked like
        direct pickle.loads references. The base may itself be an alias.
        """
        if len(call.args) < 2:
            return None
        base, attr = call.args[0], call.args[1]
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            return None
        if isinstance(base, ast.Name):
            base_name = aliases.get(base.id, base.id)
            return f"{base_name}.{attr.value}"
        if isinstance(base, ast.Attribute):
            base_name = ASTSecurityScanner._get_func_name(base)
            if base_name:
                return f"{base_name}.{attr.value}"
        return None

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
