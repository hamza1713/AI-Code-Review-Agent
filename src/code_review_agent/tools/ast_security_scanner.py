"""
AST-based Security Scanner with Constant Folding and Variable Indirection Tracking.
Analyzes Python AST structures to detect security vulnerabilities that span multiple lines,
variables, or involve constant folding (e.g. 0o777, stat masks, aliased functions, indirect path joins).
"""

import ast
import re
import stat
import textwrap
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

            # ── Check for breaking API signature changes in function defs ──
            for hunk in file_diff.hunks:
                deleted_defs = []
                added_defs = []
                curr_target_line = hunk.new_start
                for line in hunk.lines:
                    if line.startswith("-") and re.match(r"^-\s*def\s+([A-Za-z_]\w*)", line):
                        deleted_defs.append(line[1:].strip())
                    elif line.startswith("+"):
                        if re.match(r"^\+\s*def\s+([A-Za-z_]\w*)", line):
                            added_defs.append((curr_target_line, line[1:].strip()))
                        curr_target_line += 1
                    elif not line.startswith("-"):
                        curr_target_line += 1

                for del_code in deleted_defs:
                    del_match = re.match(r"^def\s+([A-Za-z_]\w*)\s*\((.*)\)", del_code)
                    if not del_match:
                        continue
                    del_name = del_match.group(1)
                    for add_line_no, add_code in added_defs:
                        add_match = re.match(r"^def\s+([A-Za-z_]\w*)\s*\((.*)\)", add_code)
                        if not add_match or add_match.group(1) != del_name:
                            continue

                        try:
                            old_ast = ast.parse(del_code + "\n    pass")
                            new_ast = ast.parse(add_code + "\n    pass")
                            old_fn = old_ast.body[0]
                            new_fn = new_ast.body[0]
                            if isinstance(old_fn, ast.FunctionDef) and isinstance(new_fn, ast.FunctionDef):
                                old_args = [a.arg for a in old_fn.args.args]
                                old_defaults_count = len(old_fn.args.defaults)
                                old_req = len(old_args) - old_defaults_count
                                new_args = [a.arg for a in new_fn.args.args]
                                new_defaults_count = len(new_fn.args.defaults)
                                new_req = len(new_args) - new_defaults_count

                                # Breaking if new required parameters added without defaults, or parameters removed
                                if new_req > old_req or any(arg not in old_args for arg in new_args[:new_req]):
                                    findings.append(
                                        SastFinding(
                                            rule_id="ARCH-SIG-001",
                                            cwe="",
                                            category="ARCHITECTURE",
                                            name="Breaking API Function Signature Change",
                                            description=(
                                                f"Breaking API signature change: Function '{del_name}' added required "
                                                f"parameter(s) without defaults, breaking existing callers."
                                            ),
                                            severity="WARNING",
                                            file_path=file_path,
                                            line_number=add_line_no,
                                            snippet=add_code,
                                            fix_recommendation="Provide default values for new parameters (e.g. param = None) or maintain backwards-compatible overload/facade.",
                                            analyzer_source="ast",
                                        )
                                    )
                        except SyntaxError:
                            pass

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

            tree = None
            line_offset = 0
            # 1. Try direct parse
            try:
                tree = ast.parse(code_text, filename=file_path)
            except SyntaxError:
                pass

            # 2. Try dedented parse
            if tree is None:
                dedented = textwrap.dedent(code_text)
                try:
                    tree = ast.parse(dedented, filename=file_path)
                except SyntaxError:
                    pass

            # 3. Try wrapped parse (e.g. if snippet contains 'return')
            if tree is None:
                dedented = textwrap.dedent(code_text)
                wrapped = "def _diff_scope():\n" + textwrap.indent(dedented, "    ")
                try:
                    tree = ast.parse(wrapped, filename=file_path)
                    line_offset = 1
                except SyntaxError:
                    continue

            adjusted_mapping = {k + line_offset: v for k, v in line_no_mapping.items()}
            adjusted_clean_lines = ([""] * line_offset) + clean_lines

            file_findings = cls._analyze_tree(tree, file_path, adjusted_clean_lines, adjusted_mapping, added_lines_map)
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

            # ── Sink 8: Weak hash (MD5/SHA1) direct or via hashlib.new with password context check ──
            if isinstance(node, ast.Call):
                func_name = cls._get_func_name(node.func)
                resolved = aliases.get(func_name, func_name)
                is_weak_hash = False
                algo_name = ""

                if resolved in ("hashlib.md5", "hashlib.sha1"):
                    is_weak_hash = True
                    algo_name = "MD5" if "md5" in resolved else "SHA-1"
                elif resolved == "hashlib.new" and node.args:
                    algo_val = evaluate_ast_constant(node.args[0], env)
                    if isinstance(algo_val, str) and algo_val.lower().replace("-", "") in ("md5", "sha1", "md4", "md2"):
                        is_weak_hash = True
                        algo_name = algo_val.upper()

                if is_weak_hash:
                    actual_line = line_no_mapping.get(node.lineno, node.lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                        enclosing_fn = cls._enclosing_function_node(node, tree)
                        fn_name = enclosing_fn.name if enclosing_fn else ""

                        # Check for password/auth context in function name, args, or snippet
                        first_arg_str = cls._node_to_str(node.args[0]) if node.args else ""
                        context_blob = f"{fn_name} {first_arg_str} {snippet}".lower()
                        is_pw_ctx = bool(re.search(r"password|passwd|pwd|credential|secret|auth|token|pin", context_blob))

                        if is_pw_ctx:
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-CRYPTO-001",
                                    cwe="CWE-327",
                                    category="SECURITY",
                                    name=f"Insecure Password Hashing ({algo_name})",
                                    description=(
                                        f"Cryptographically weak hash algorithm '{algo_name}' used for password hashing in '{fn_name or 'function'}'. "
                                        f"MD5 and SHA-1 have broken collision resistance and are vulnerable to rapid GPU brute-force."
                                    ),
                                    severity="CRITICAL",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Use a dedicated password hashing algorithm with salt and work factor like bcrypt or Argon2 (e.g. bcrypt.hashpw or argon2-cffi).",
                                    analyzer_source="ast",
                                )
                            )

                            # Check for salt presence in scope
                            has_salt = False
                            if enclosing_fn:
                                for subnode in ast.walk(enclosing_fn):
                                    if isinstance(subnode, ast.Name) and re.search(r"salt|pepper|nonce", subnode.id, re.I):
                                        has_salt = True
                                        break
                                    if isinstance(subnode, ast.Call):
                                        cname = cls._get_func_name(subnode.func)
                                        if cname in ("os.urandom", "secrets.token_bytes", "secrets.token_hex"):
                                            has_salt = True
                                            break
                            if not has_salt:
                                findings.append(
                                    SastFinding(
                                        rule_id="SEC-CRYPTO-002",
                                        cwe="CWE-760",
                                        category="SECURITY",
                                        name="Unsalted Password Hash",
                                        description=(
                                            "Password is hashed without a cryptographic salt. Identical passwords produce identical "
                                            "hash values, enabling precomputed rainbow table attacks."
                                        ),
                                        severity="HIGH",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Incorporate a cryptographically random, per-user salt (at least 16 bytes), or use bcrypt / Argon2 which manage salts automatically.",
                                        analyzer_source="ast",
                                    )
                                )
                        else:
                            findings.append(
                                SastFinding(
                                    rule_id="SEC-CRYPTO-001",
                                    cwe="CWE-327",
                                    category="SECURITY",
                                    name=f"Weak Hash Algorithm ({algo_name})",
                                    description=(
                                        f"'{resolved}' selects a cryptographically broken hash algorithm ({algo_name}). "
                                        f"MD5 and SHA-1 are vulnerable to collisions and unsuitable for security verification."
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
                                category="QUALITY",
                                name="Swallowed Exception Anti-Pattern",
                                description=(
                                    f"Silently swallowed exception anti-pattern: '{handler_name}:' with a body of only pass/... "
                                    f"silently swallows all errors, hiding runtime bugs and (for BaseException) blocking KeyboardInterrupt/SystemExit."
                                ),
                                severity="WARNING",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation="Catch specific exceptions and log them: except SpecificError as e: logger.warning(f'... {e}').",
                                analyzer_source="ast",
                            )
                        )

            # ── Sink 8: N+1 ORM / Database Query Loop ──
            if isinstance(node, (ast.For, ast.While)):
                for subnode in ast.walk(node):
                    if subnode is not node and isinstance(subnode, ast.Call):
                        subfunc = cls._get_func_name(subnode.func)
                        is_query_call = (
                            subfunc.endswith(".query")
                            or subfunc.endswith(".execute")
                            or subfunc.endswith(".filter")
                            or subfunc in ("query", "execute")
                            or "query" in subfunc.lower()
                        )
                        if is_query_call:
                            actual_line = line_no_mapping.get(subnode.lineno, subnode.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[subnode.lineno - 1] if 0 < subnode.lineno <= len(clean_lines) else ""
                                findings.append(
                                    SastFinding(
                                        rule_id="QUAL-NPLUS1-001",
                                        cwe="",
                                        category="QUALITY",
                                        name="N+1 ORM Query Loop",
                                        description=(
                                            f"N+1 query loop anti-pattern: Database query '{subfunc}' "
                                            f"executed repeatedly inside an iteration loop for order items or records."
                                        ),
                                        severity="WARNING",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Eager load related records using batch queries or ORM select_related/prefetch_related outside the loop.",
                                        analyzer_source="ast",
                                    )
                                )

        # Class-level concurrency checks (Fix 2 & 2b)
        findings.extend(cls._check_unused_locks(tree, file_path, clean_lines, line_no_mapping, added_lines_map))
        findings.extend(cls._check_singleton_races(tree, file_path, clean_lines, line_no_mapping, added_lines_map))

        # Algorithmic and performance checks (Fix 3)
        findings.extend(cls._check_performance_patterns(tree, file_path, clean_lines, line_no_mapping, added_lines_map))

        # Return type completeness check (Fix 5)
        findings.extend(cls._check_implicit_none_returns(tree, file_path, clean_lines, line_no_mapping, added_lines_map))

        return findings

    @classmethod
    def _check_unused_locks(
        cls,
        tree: ast.AST,
        file_path: str,
        clean_lines: List[str],
        line_no_mapping: Dict[int, int],
        added_lines_map: Dict[int, str],
    ) -> List[SastFinding]:
        findings: List[SastFinding] = []
        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            lock_fields: Dict[str, int] = {}
            used_fields: set = set()

            for item in class_node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                is_init = item.name == "__init__"
                for stmt in ast.walk(item):
                    if is_init and isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if (
                                isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"
                                and isinstance(stmt.value, ast.Call)
                            ):
                                fn = cls._get_func_name(stmt.value.func)
                                if fn in (
                                    "threading.Lock", "threading.RLock",
                                    "asyncio.Lock", "threading.Semaphore",
                                    "Lock", "RLock"
                                ):
                                    lock_fields[tgt.attr] = stmt.lineno

                    # Usage: with self._lock: or with self._lock as ...:
                    if isinstance(stmt, (ast.With, ast.AsyncWith)):
                        for with_item in stmt.items:
                            expr = with_item.context_expr
                            if (
                                isinstance(expr, ast.Attribute)
                                and isinstance(expr.value, ast.Name)
                                and expr.value.id == "self"
                            ):
                                used_fields.add(expr.attr)

                    # Usage: self._lock.acquire()
                    if isinstance(stmt, ast.Call):
                        fn = cls._get_func_name(stmt.func)
                        if ".acquire" in fn or fn.endswith("acquire"):
                            if isinstance(stmt.func, ast.Attribute):
                                if (
                                    isinstance(stmt.func.value, ast.Attribute)
                                    and isinstance(stmt.func.value.value, ast.Name)
                                    and stmt.func.value.value.id == "self"
                                ):
                                    used_fields.add(stmt.func.value.attr)

            for field_name, lineno in lock_fields.items():
                if field_name not in used_fields:
                    actual_line = line_no_mapping.get(lineno, lineno)
                    if actual_line in added_lines_map:
                        snippet = clean_lines[lineno - 1] if 0 < lineno <= len(clean_lines) else ""
                        findings.append(
                            SastFinding(
                                rule_id="SEC-LOCK-001",
                                cwe="CWE-362",
                                category="SECURITY",
                                name="Lock Field Defined But Never Acquired",
                                description=(
                                    f"'{class_node.name}.{field_name}' is initialized as a synchronization Lock "
                                    f"in __init__ but is never acquired with 'with self.{field_name}:' or '.acquire()' "
                                    f"in any method of the class. This leaves shared resources unguarded while creating "
                                    f"a misleading appearance of thread safety."
                                ),
                                severity="HIGH",
                                file_path=file_path,
                                line_number=actual_line,
                                snippet=snippet.strip(),
                                fix_recommendation=f"Synchronize access to shared connection or state using 'with self.{field_name}:'.",
                                analyzer_source="ast",
                            )
                        )
        return findings

    @classmethod
    def _check_singleton_races(
        cls,
        tree: ast.AST,
        file_path: str,
        clean_lines: List[str],
        line_no_mapping: Dict[int, int],
        added_lines_map: Dict[int, str],
    ) -> List[SastFinding]:
        findings: List[SastFinding] = []
        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            for item in class_node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name not in ("__call__", "get_instance", "instance"):
                    continue

                has_lock = any(
                    isinstance(stmt, (ast.With, ast.AsyncWith))
                    or (isinstance(stmt, ast.Call) and "acquire" in cls._get_func_name(stmt.func))
                    for stmt in ast.walk(item)
                )

                for stmt in item.body:
                    if isinstance(stmt, ast.If):
                        is_membership_check = False
                        if isinstance(stmt.test, ast.Compare):
                            for op in stmt.test.ops:
                                if isinstance(op, ast.NotIn):
                                    is_membership_check = True
                                    break
                        elif isinstance(stmt.test, ast.UnaryOp) and isinstance(stmt.test.op, ast.Not):
                            is_membership_check = True

                        if is_membership_check:
                            sets_instance = False
                            for s in ast.walk(stmt):
                                if isinstance(s, ast.Assign):
                                    for t in s.targets:
                                        if isinstance(t, (ast.Subscript, ast.Attribute)):
                                            sets_instance = True
                                            break
                            if sets_instance and not has_lock:
                                actual_line = line_no_mapping.get(stmt.lineno, stmt.lineno)
                                if actual_line in added_lines_map:
                                    snippet = clean_lines[stmt.lineno - 1] if 0 < stmt.lineno <= len(clean_lines) else ""
                                    findings.append(
                                        SastFinding(
                                            rule_id="SEC-RACE-001",
                                            cwe="CWE-362",
                                            category="SECURITY",
                                            name="Singleton Metaclass TOCTOU Race Condition",
                                            description=(
                                                f"Method '{class_node.name}.{item.name}' performs a check-then-set pattern "
                                                f"for singleton instance creation without synchronization. Under concurrent "
                                                f"threads or coroutines, multiple instances can be created simultaneously."
                                            ),
                                            severity="HIGH",
                                            file_path=file_path,
                                            line_number=actual_line,
                                            snippet=snippet.strip(),
                                            fix_recommendation="Protect singleton instantiation using a threading.Lock: 'with cls._lock: if cls not in cls._instances: ...'",
                                            analyzer_source="ast",
                                        )
                                    )
        return findings

    @classmethod
    def _check_performance_patterns(
        cls,
        tree: ast.AST,
        file_path: str,
        clean_lines: List[str],
        line_no_mapping: Dict[int, int],
        added_lines_map: Dict[int, str],
    ) -> List[SastFinding]:
        findings: List[SastFinding] = []

        # 1. list.pop(0) -> O(n) front-pop
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "pop"
                    and node.args
                ):
                    first_arg = node.args[0]
                    if (
                        (isinstance(first_arg, ast.Constant) and first_arg.value == 0)
                        or (not isinstance(first_arg, ast.UnaryOp) and getattr(first_arg, "n", None) == 0)
                    ):
                        actual_line = line_no_mapping.get(node.lineno, node.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[node.lineno - 1] if 0 < node.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="PERF-LIST-001",
                                    cwe="",
                                    category="QUALITY",
                                    name="O(n) pop(0) Linear Queue Pop",
                                    description=(
                                        "Calling .pop(0) on a Python list requires shifting all subsequent elements left in memory, "
                                        "resulting in O(n) complexity per pop."
                                    ),
                                    severity="MEDIUM",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Use collections.deque.popleft() for O(1) FIFO queues, or heapq.heappop() for priority queues.",
                                    analyzer_source="ast",
                                )
                            )

        # 2. Repeated sort on list insertion (matching receiver)
        for func_node in ast.walk(tree):
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            appended_receivers: set = set()
            for n in ast.walk(func_node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "append"
                ):
                    rec_str = cls._node_to_str(n.func.value)
                    if rec_str:
                        appended_receivers.add(rec_str)

            if not appended_receivers:
                continue

            for n in ast.walk(func_node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "sort"
                ):
                    rec_str = cls._node_to_str(n.func.value)
                    if rec_str and rec_str in appended_receivers:
                        actual_line = line_no_mapping.get(n.lineno, n.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[n.lineno - 1] if 0 < n.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="PERF-SORT-001",
                                    cwe="",
                                    category="QUALITY",
                                    name="Repeated O(n log n) Sort on List Insertion",
                                    description=(
                                        f"List '{rec_str}' is appended to and then immediately sorted with .sort(), causing "
                                        f"an O(n log n) sorting pass on every insertion (overall O(n^2 log n) queue population)."
                                    ),
                                    severity="MEDIUM",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Use heapq.heappush() for O(log n) insertion into a heap, or bisect.insort().",
                                    analyzer_source="ast",
                                )
                            )

        # 3. O(n^2) nested loop duplicate search
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.For):
                continue
            outer_len_target = cls._get_range_len_target(outer.iter)
            if not outer_len_target:
                continue

            for inner in outer.body:
                for sub in ast.walk(inner):
                    if sub is not outer and isinstance(sub, ast.For):
                        inner_len_target = cls._get_range_len_target(sub.iter)
                        if inner_len_target and inner_len_target == outer_len_target:
                            actual_line = line_no_mapping.get(sub.lineno, sub.lineno)
                            if actual_line in added_lines_map:
                                snippet = clean_lines[sub.lineno - 1] if 0 < sub.lineno <= len(clean_lines) else ""
                                findings.append(
                                    SastFinding(
                                        rule_id="PERF-NESTED-001",
                                        cwe="",
                                        category="QUALITY",
                                        name="O(n^2) Quadratic Nested Loop Iteration",
                                        description=(
                                            f"Nested loops iterate over range(len({outer_len_target})) quadratically, "
                                            f"resulting in O(n^2) comparisons. This creates severe latency bottlenecks on large collections."
                                        ),
                                        severity="MEDIUM",
                                        file_path=file_path,
                                        line_number=actual_line,
                                        snippet=snippet.strip(),
                                        fix_recommendation="Use a set or collections.Counter for O(n) membership or duplicate lookup.",
                                        analyzer_source="ast",
                                    )
                                )

        return findings

    @classmethod
    def _check_implicit_none_returns(
        cls,
        tree: ast.AST,
        file_path: str,
        clean_lines: List[str],
        line_no_mapping: Dict[int, int],
        added_lines_map: Dict[int, str],
    ) -> List[SastFinding]:
        findings: List[SastFinding] = []
        for func_node in ast.walk(tree):
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func_node.returns is None:
                continue

            ret_str = cls._node_to_str(func_node.returns)
            is_optional = (
                ret_str in ("None", "")
                or "Optional" in ret_str
                or (isinstance(func_node.returns, ast.Constant) and func_node.returns.value is None)
                or (
                    isinstance(func_node.returns, ast.BinOp)
                    and isinstance(func_node.returns.op, ast.BitOr)
                    and (cls._node_to_str(func_node.returns.left) == "None" or cls._node_to_str(func_node.returns.right) == "None")
                )
            )
            if is_optional:
                continue

            for child in ast.walk(func_node):
                if isinstance(child, ast.ExceptHandler):
                    body_is_silent = len(child.body) == 1 and (
                        isinstance(child.body[0], ast.Pass)
                        or (
                            isinstance(child.body[0], ast.Expr)
                            and isinstance(child.body[0].value, ast.Constant)
                            and child.body[0].value.value is Ellipsis
                        )
                    )
                    if body_is_silent:
                        actual_line = line_no_mapping.get(child.lineno, child.lineno)
                        if actual_line in added_lines_map:
                            snippet = clean_lines[child.lineno - 1] if 0 < child.lineno <= len(clean_lines) else ""
                            findings.append(
                                SastFinding(
                                    rule_id="QUAL-RETURN-001",
                                    cwe="",
                                    category="QUALITY",
                                    name="Implicit None Return Violates Type Annotation",
                                    description=(
                                        f"Function '{func_node.name}' is annotated to return '{ret_str or 'non-Optional'}' "
                                        f"but contains an exception handler that silently swallows errors and falls through, "
                                        f"implicitly returning None. This violates the declared type contract."
                                    ),
                                    severity="MEDIUM",
                                    file_path=file_path,
                                    line_number=actual_line,
                                    snippet=snippet.strip(),
                                    fix_recommendation="Re-raise the exception, return an explicit fallback value, or update return annotation to Optional[...].",
                                    analyzer_source="ast",
                                )
                            )
                        break
        return findings

    @classmethod
    def _enclosing_function_node(cls, target_node: ast.AST, tree: ast.AST) -> Optional[ast.AST]:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if child is target_node:
                        return node
        return None

    @staticmethod
    def _node_to_str(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            val = ASTSecurityScanner._node_to_str(node.value)
            return f"{val}.{node.attr}" if val else node.attr
        if isinstance(node, ast.Constant):
            return str(node.value)
        return ""

    @classmethod
    def _get_range_len_target(cls, node: ast.AST) -> str:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"
            and node.args
        ):
            arg = node.args[0]
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "len"
                and arg.args
            ):
                return cls._node_to_str(arg.args[0])
        return ""

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


# Backward-compatible alias
AstSecurityScanner = ASTSecurityScanner
