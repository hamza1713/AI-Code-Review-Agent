"""
AST Code Graph Indexer and Cross-File Dependency Analyzer.
Parses repository Python ASTs to build symbol tables, call graphs (callers/callees),
and cross-file impact maps for modified PR functions with qualified symbol resolution.
"""

import ast
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from code_review_agent.config import logger
from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import CodeGraphSummary, SymbolInfo

try:
    import tree_sitter_languages

    _TREE_SITTER_AVAILABLE = True
except ImportError:
    _TREE_SITTER_AVAILABLE = False

_EXT_TO_TS_LANG: dict[str, str] = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}

# Thread-safe global cache for indexed code graphs
_INDEX_CACHE: dict[
    str,
    tuple[
        float,
        dict[str, SymbolInfo],
        dict[str, list[SymbolInfo]],
        dict[str, set[str]],
        dict[str, set[str]],
        int,
    ],
] = {}
_INDEX_CACHE_LOCK = threading.Lock()
INDEX_CACHE_TTL_SECONDS = 300.0  # 5 minutes


class CodeGraphIndexer:
    """
    Indexes codebase symbols, function calls, and module dependencies.
    Key symbols by fully-qualified identifier (e.g. 'pkg/module.py:ClassName.function_name')
    to prevent cross-file namespace collisions and inaccurate caller reporting.
    """

    @classmethod
    def clear_cache(cls):
        """Thread-safe purge of the in-memory global AST index cache."""
        with _INDEX_CACHE_LOCK:
            _INDEX_CACHE.clear()

    def __init__(self, repo_root: str | None = None):
        self.repo_root = Path(repo_root or ".").resolve()
        # qualified_name (e.g. "path/file.py:ClassName.func") -> SymbolInfo
        self.symbols: dict[str, SymbolInfo] = {}
        # bare_name -> List[SymbolInfo] (for multi-file lookup without collision)
        self.symbols_by_name: dict[str, list[SymbolInfo]] = {}
        # qualified_caller -> set(callee_identifiers)
        self.call_graph: dict[str, set[str]] = {}
        # callee_identifier -> set(qualified_callers)
        self.reverse_call_graph: dict[str, set[str]] = {}
        self.indexed_files_count: int = 0

    def _get_disk_cache_path(self, max_files: int) -> Path:
        cache_id = hashlib.sha256(
            f"{str(self.repo_root)}::{max_files}".encode()
        ).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f"code_graph_cache_{cache_id}.json"

    def _load_from_disk_cache(self, disk_path: Path, max_age: float) -> bool:
        if not disk_path.exists():
            return False
        try:
            mtime = disk_path.stat().st_mtime
            if (time.time() - mtime) > max_age:
                return False
            data = json.loads(disk_path.read_text(encoding="utf-8"))
            self.symbols = {
                k: SymbolInfo(**v) for k, v in data.get("symbols", {}).items()
            }
            self.symbols_by_name = {
                k: [SymbolInfo(**x) for x in v]
                for k, v in data.get("symbols_by_name", {}).items()
            }
            self.call_graph = {k: set(v) for k, v in data.get("call_graph", {}).items()}
            self.reverse_call_graph = {
                k: set(v) for k, v in data.get("reverse_call_graph", {}).items()
            }
            self.indexed_files_count = data.get("indexed_files_count", 0)
            return True
        except Exception as e:
            logger.debug(f"Failed to load code graph from disk cache {disk_path}: {e}")
            return False

    def _save_to_disk_cache(self, disk_path: Path):
        try:
            payload = {
                "symbols": {k: v.model_dump() for k, v in self.symbols.items()},
                "symbols_by_name": {
                    k: [x.model_dump() for x in v]
                    for k, v in self.symbols_by_name.items()
                },
                "call_graph": {k: list(v) for k, v in self.call_graph.items()},
                "reverse_call_graph": {
                    k: list(v) for k, v in self.reverse_call_graph.items()
                },
                "indexed_files_count": self.indexed_files_count,
            }
            tmp_path = disk_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            tmp_path.replace(disk_path)
        except Exception as e:
            logger.debug(f"Failed to write code graph to disk cache {disk_path}: {e}")

    def index_repository(
        self, max_files: int = 500, force_reindex: bool = False
    ) -> CodeGraphSummary:
        """Scan and index all python source files in repository with TTL caching."""
        cache_key = f"{str(self.repo_root)}::{max_files}"
        disk_cache_path = self._get_disk_cache_path(max_files)
        now = time.time()

        if not force_reindex:
            # 1. In-memory cache check
            with _INDEX_CACHE_LOCK:
                cached = _INDEX_CACHE.get(cache_key)
                if cached and (now - cached[0]) < INDEX_CACHE_TTL_SECONDS:
                    (
                        _,
                        self.symbols,
                        self.symbols_by_name,
                        self.call_graph,
                        self.reverse_call_graph,
                        self.indexed_files_count,
                    ) = cached
                    logger.debug(
                        f"🕸️ Code Graph Indexer: Reused in-memory graph for {self.repo_root} ({len(self.symbols)} symbols)."
                    )
                    return CodeGraphSummary(
                        files_indexed=self.indexed_files_count,
                        symbols_count=len(self.symbols),
                        impacted_callers={},
                    )
            # 2. On-disk cache check across subprocess workers
            if self._load_from_disk_cache(
                disk_cache_path, max_age=INDEX_CACHE_TTL_SECONDS
            ):
                with _INDEX_CACHE_LOCK:
                    _INDEX_CACHE[cache_key] = (
                        now,
                        dict(self.symbols),
                        dict(self.symbols_by_name),
                        dict(self.call_graph),
                        dict(self.reverse_call_graph),
                        self.indexed_files_count,
                    )
                logger.debug(
                    f"🕸️ Code Graph Indexer: Reused disk-cached graph for {self.repo_root} ({len(self.symbols)} symbols)."
                )
                return CodeGraphSummary(
                    files_indexed=self.indexed_files_count,
                    symbols_count=len(self.symbols),
                    impacted_callers={},
                )

        self.symbols.clear()
        self.symbols_by_name.clear()
        self.call_graph.clear()
        self.reverse_call_graph.clear()
        self.indexed_files_count = 0

        supported_exts = {
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".go",
            ".java",
            ".rs",
        }
        source_files: list[Path] = []
        for root, dirs, files in os.walk(self.repo_root):
            # Skip hidden and cache folders
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d
                not in [
                    "__pycache__",
                    "venv",
                    ".venv",
                    "node_modules",
                    "build",
                    "dist",
                    ".git",
                    "vendor",
                ]
            ]
            for f in files:
                if Path(f).suffix.lower() in supported_exts:
                    source_files.append(Path(root) / f)
                    if len(source_files) >= max_files:
                        break

        for file_path in source_files:
            self._index_file(file_path)

        self.indexed_files_count = len(source_files)

        with _INDEX_CACHE_LOCK:
            _INDEX_CACHE[cache_key] = (
                now,
                dict(self.symbols),
                dict(self.symbols_by_name),
                dict(self.call_graph),
                dict(self.reverse_call_graph),
                self.indexed_files_count,
            )
        self._save_to_disk_cache(disk_cache_path)

        logger.info(
            f"🕸️ Code Graph Indexer: Indexed {len(self.symbols)} qualified symbols across {self.indexed_files_count} files."
        )

        return CodeGraphSummary(
            files_indexed=self.indexed_files_count,
            symbols_count=len(self.symbols),
            impacted_callers={},
        )

    def _index_file(self, file_path: Path):
        """Parse symbols, scopes, and function calls from a file (Python, JS, TS, Go, Java)."""
        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            logger.debug(f"Could not read {file_path}: {e}")
            return

        try:
            rel_path = str(file_path.relative_to(self.repo_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")

        ext = file_path.suffix.lower()

        if ext == ".py":
            try:
                tree = ast.parse(content, filename=str(file_path))
                import_map = self._extract_imports(tree, rel_path)
                self._index_ast_nodes(tree, rel_path, import_map, scope_prefix="")
            except Exception as e:
                logger.debug(f"Could not parse Python AST for {file_path}: {e}")
        elif ext in [".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rs"]:
            self._index_non_python_file(content, rel_path, ext)

    def _find_block_end(self, lines: list[str], start_idx: int) -> int:
        """Compute block end line number using brace depth tracking."""
        depth = 0
        found_open = False
        for idx in range(start_idx, len(lines)):
            line_text = lines[idx]
            cleaned = re.sub(r"//.*$|/\*.*?\*/|['\"`].*?['\"`]", "", line_text)
            depth += cleaned.count("{")
            if "{" in cleaned:
                found_open = True
            depth -= cleaned.count("}")
            if found_open and depth <= 0:
                return idx + 1
        return min(len(lines), start_idx + 1)

    def _index_non_python_file(self, content: str, rel_path: str, ext: str):
        """
        Extract symbols and function calls from non-Python files (.js, .jsx, .ts, .tsx, .go, .java, .rs).
        Uses Tree-Sitter AST parsing for semantic precision, falling back seamlessly to regex matching.
        """
        if _TREE_SITTER_AVAILABLE and ext in _EXT_TO_TS_LANG:
            try:
                if self._index_non_python_tree_sitter(content, rel_path, ext):
                    return
            except Exception as e:
                logger.debug(
                    f"Tree-sitter parse failed for {rel_path}: {e}; falling back to regex"
                )

        self._index_non_python_regex(content, rel_path, ext)

    def _index_non_python_tree_sitter(
        self, content: str, rel_path: str, ext: str
    ) -> bool:
        """Parse non-Python files using polyglot Tree-Sitter grammar ASTs."""
        ts_lang = _EXT_TO_TS_LANG.get(ext)
        if not ts_lang:
            return False

        parser = tree_sitter_languages.get_parser(ts_lang)
        code_bytes = content.encode("utf-8", errors="ignore")
        tree = parser.parse(code_bytes)

        def _get_text(node) -> str:
            return code_bytes[node.start_byte : node.end_byte].decode(
                "utf-8", errors="ignore"
            )

        def _extract_params(param_node) -> list[str]:
            params: list[str] = []
            if not param_node:
                return params
            for child in param_node.children:
                if child.type in (",", "(", ")", "{", "}", ";", ":"):
                    continue
                if child.type in ("identifier", "name"):
                    params.append(_get_text(child))
                elif child.type in ("required_parameter", "optional_parameter"):
                    p_name = child.child_by_field_name(
                        "pattern"
                    ) or child.child_by_field_name("name")
                    params.append(
                        _get_text(p_name)
                        if p_name
                        else _get_text(child).split(":")[0].strip()
                    )
                elif child.type == "parameter_declaration":
                    p_name = child.child_by_field_name("name")
                    params.append(
                        _get_text(p_name)
                        if p_name
                        else _get_text(child).split(" ")[0].strip()
                    )
                elif child.type == "formal_parameter":
                    p_name = child.child_by_field_name("name")
                    params.append(
                        _get_text(p_name)
                        if p_name
                        else _get_text(child).split()[-1].strip()
                    )
                else:
                    txt = _get_text(child).split(":")[0].split()[0].strip()
                    if txt and txt not in (",", "(", ")", "{", "}"):
                        params.append(txt)
            return [
                p for p in params if p and p not in (",", "(", ")", "self", "this")
            ]

        symbols_found: list[dict] = []
        calls_found: list[tuple[str, int]] = []

        def _walk(node):
            ntype = node.type
            sym_name = None
            kind = "function"
            params: list[str] = []

            if ntype in ("function_declaration", "function_item"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    sym_name = _get_text(name_node)
                    params = _extract_params(node.child_by_field_name("parameters"))
            elif ntype in ("method_declaration", "method_definition"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    sym_name = _get_text(name_node)
                    kind = "method"
                    params = _extract_params(node.child_by_field_name("parameters"))
            elif ntype in (
                "class_declaration",
                "struct_item",
                "interface_declaration",
            ):
                name_node = node.child_by_field_name("name")
                if name_node:
                    sym_name = _get_text(name_node)
                    kind = "class"
            elif ntype == "variable_declarator":
                val = node.child_by_field_name("value")
                if val and val.type in ("arrow_function", "function_expression"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        sym_name = _get_text(name_node)
                        params = _extract_params(val.child_by_field_name("parameters"))

            if sym_name:
                symbols_found.append(
                    {
                        "name": sym_name,
                        "kind": kind,
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "params": params,
                    }
                )

            if ntype in ("call_expression", "method_invocation"):
                fn_node = node.child_by_field_name(
                    "function"
                ) or node.child_by_field_name("name")
                if fn_node:
                    if fn_node.type == "member_expression":
                        prop = fn_node.child_by_field_name("property")
                        cname = _get_text(prop) if prop else _get_text(fn_node)
                    elif fn_node.type == "field_expression":
                        field = fn_node.child_by_field_name("field")
                        cname = _get_text(field) if field else _get_text(fn_node)
                    else:
                        cname = _get_text(fn_node)

                    if cname and cname not in (
                        "if",
                        "for",
                        "while",
                        "switch",
                        "catch",
                        "import",
                        "require",
                        "make",
                        "new",
                        "super",
                        "this",
                        "return",
                        "func",
                        "fmt",
                    ):
                        calls_found.append((cname, node.start_point[0] + 1))

            for child in node.children:
                _walk(child)

        _walk(tree.root_node)

        if not symbols_found and not calls_found:
            return False

        for sym_data in symbols_found:
            fn_name = sym_data["name"]
            kind = sym_data["kind"]
            qname = f"{rel_path}:{fn_name}"
            l_start = sym_data["line_start"]
            l_end = sym_data["line_end"]

            callees = set()
            for callee_name, c_line in calls_found:
                if l_start <= c_line <= l_end:
                    callees.add(callee_name)

            sym = SymbolInfo(
                name=fn_name,
                qualified_name=qname,
                kind=kind,
                file_path=rel_path,
                line_start=l_start,
                line_end=l_end,
                parameters=sym_data["params"],
                callers=[],
                callees=sorted(callees),
            )
            self.symbols[qname] = sym
            self.symbols_by_name.setdefault(fn_name, []).append(sym)
            if callees:
                self.call_graph[qname] = callees
                for callee in callees:
                    self.reverse_call_graph.setdefault(callee, set()).add(qname)
                    self.reverse_call_graph.setdefault(
                        f"{rel_path}:{callee}", set()
                    ).add(qname)

        for callee_name, c_line in calls_found:
            self.reverse_call_graph.setdefault(callee_name, set()).add(
                f"{rel_path}:L{c_line}"
            )
            self.reverse_call_graph.setdefault(f"{rel_path}:{callee_name}", set()).add(
                f"{rel_path}:L{c_line}"
            )

        return True

    def _index_non_python_regex(self, content: str, rel_path: str, ext: str):
        """Extract symbols and function calls from non-Python files using structured regex fallback matching."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            fn_name = None
            kind = "function"
            params = []

            # Check if line looks like beginning of multi-line function/method
            sig_candidate = line
            if (
                any(kw in line for kw in ("function", "=>", "func", "class", "fn "))
                or "(" in line
            ):
                buf = [line]
                for forward_idx in range(i, min(len(lines), i + 5)):
                    if ")" in buf[-1] or "{" in buf[-1]:
                        break
                    buf.append(lines[forward_idx])
                sig_candidate = " ".join(buf)

            if ext in [".js", ".jsx", ".ts", ".tsx"]:
                fn_m = re.search(
                    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)",
                    sig_candidate,
                )
                arrow_m = re.search(
                    r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>",
                    sig_candidate,
                )
                class_m = re.search(
                    r"(?:export\s+)?class\s+([A-Za-z0-9_$]+)", sig_candidate
                )
                if fn_m:
                    fn_name = fn_m.group(1)
                    params = [
                        p.strip().split(":")[0]
                        for p in fn_m.group(2).split(",")
                        if p.strip()
                    ]
                elif arrow_m:
                    fn_name = arrow_m.group(1)
                    params = [
                        p.strip().split(":")[0]
                        for p in arrow_m.group(2).split(",")
                        if p.strip()
                    ]
                elif class_m:
                    fn_name = class_m.group(1)
                    kind = "class"
            elif ext == ".go":
                go_m = re.search(
                    r"func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\((.*?)\)",
                    sig_candidate,
                )
                if go_m:
                    fn_name = go_m.group(1)
                    params = [
                        p.strip().split(" ")[0]
                        for p in go_m.group(2).split(",")
                        if p.strip()
                    ]
            elif ext == ".java":
                java_m = re.search(
                    r"(?:public|protected|private|static|\s)+\s+[\w\<\>\[\]]+\s+([A-Za-z0-9_]+)\s*\((.*?)\)\s*\{?",
                    sig_candidate,
                )
                class_m = re.search(
                    r"(?:public\s+)?class\s+([A-Za-z0-9_]+)", sig_candidate
                )
                if java_m and java_m.group(1) not in ["if", "for", "while", "switch"]:
                    fn_name = java_m.group(1)
                    kind = "method"
                elif class_m:
                    fn_name = class_m.group(1)
                    kind = "class"
            elif ext == ".rs":
                rs_m = re.search(
                    r"(?:pub\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<[^>]+>)?\s*\((.*?)\)",
                    sig_candidate,
                )
                if rs_m:
                    fn_name = rs_m.group(1)
                    params = [
                        p.strip().split(":")[0]
                        for p in rs_m.group(2).split(",")
                        if p.strip()
                    ]

            if fn_name:
                qname = f"{rel_path}:{fn_name}"
                block_end = self._find_block_end(lines, i - 1)
                sym = SymbolInfo(
                    name=fn_name,
                    qualified_name=qname,
                    kind=kind,
                    file_path=rel_path,
                    line_start=i,
                    line_end=block_end,
                    parameters=params,
                    callers=[],
                    callees=[],
                )
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(fn_name, []).append(sym)

            # Extract call expressions
            for call_m in re.finditer(r"\b([A-Za-z0-9_$]+)\s*\(", line):
                callee = call_m.group(1)
                if callee not in [
                    "if",
                    "for",
                    "while",
                    "switch",
                    "catch",
                    "function",
                    "return",
                    "import",
                    "require",
                    "func",
                    "make",
                    "new",
                    "super",
                    "this",
                ]:
                    self.reverse_call_graph.setdefault(callee, set()).add(
                        f"{rel_path}:L{i}"
                    )
                    self.reverse_call_graph.setdefault(
                        f"{rel_path}:{callee}", set()
                    ).add(f"{rel_path}:L{i}")

    def _extract_imports(self, tree: ast.AST, rel_path: str) -> dict[str, str]:
        """Extract explicit imports in a file to assist in qualified callee resolution."""
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    asname = alias.asname or name
                    imports[asname] = name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    name = alias.name
                    asname = alias.asname or name
                    if module:
                        target = f"{module}.{name}"
                    else:
                        target = name
                    imports[asname] = target
        return imports

    def _index_ast_nodes(
        self,
        parent_node: ast.AST,
        rel_path: str,
        import_map: dict[str, str],
        scope_prefix: str = "",
    ):
        """Recursively index AST nodes maintaining class and function scope."""
        for node in parent_node.body if hasattr(parent_node, "body") else []:
            if isinstance(node, ast.ClassDef):
                class_name = node.name
                class_qualified_name = f"{rel_path}:{class_name}"
                docstring = ast.get_docstring(node)

                sym = SymbolInfo(
                    name=class_name,
                    qualified_name=class_qualified_name,
                    kind="class",
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    docstring=docstring,
                    parameters=[],
                    callers=[],
                    callees=[],
                )
                self.symbols[class_qualified_name] = sym
                self.symbols_by_name.setdefault(class_name, []).append(sym)

                # Index methods inside class
                self._index_ast_nodes(
                    node, rel_path, import_map, scope_prefix=f"{class_name}."
                )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                scoped_name = f"{scope_prefix}{func_name}"
                qualified_name = f"{rel_path}:{scoped_name}"
                kind = "method" if scope_prefix else "function"

                params = [arg.arg for arg in node.args.args]
                docstring = ast.get_docstring(node)

                # Extract calls inside this function
                callees: set[str] = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        callee_target = self._resolve_callee(
                            inner.func, rel_path, import_map
                        )
                        if callee_target:
                            callees.add(callee_target)

                sym = SymbolInfo(
                    name=func_name,
                    qualified_name=qualified_name,
                    kind=kind,
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    docstring=docstring,
                    parameters=params,
                    callers=[],
                    callees=list(callees),
                )
                self.symbols[qualified_name] = sym
                self.symbols_by_name.setdefault(func_name, []).append(sym)

                self.call_graph[qualified_name] = callees
                for callee in callees:
                    self.reverse_call_graph.setdefault(callee, set()).add(
                        qualified_name
                    )

                # Index nested functions
                self._index_ast_nodes(
                    node, rel_path, import_map, scope_prefix=f"{scoped_name}."
                )

    def _resolve_callee(
        self, node: ast.AST, rel_path: str, import_map: dict[str, str]
    ) -> str | None:
        """
        Resolve call node to a callee identifier.
        Prefers local file qualification or imported target to prevent collision.
        """
        if isinstance(node, ast.Name):
            callee_name = node.id
            if callee_name in import_map:
                return import_map[callee_name]
            # Same-file call
            return f"{rel_path}:{callee_name}"
        elif isinstance(node, ast.Attribute):
            attr_name = node.attr
            if isinstance(node.value, ast.Name):
                base_name = node.value.id
                if base_name in import_map:
                    return f"{import_map[base_name]}.{attr_name}"
                return f"{rel_path}:{base_name}.{attr_name}"
            return attr_name
        return None

    def get_impacted_callers(
        self, target_identifiers: list[str]
    ) -> dict[str, list[str]]:
        """
        Identify all external functions that call any of the specified functions or symbols.
        Supports both qualified identifiers ('file.py:func') and bare names ('func').
        """
        impact_map: dict[str, list[str]] = {}

        for target in target_identifiers:
            callers: set[str] = set()

            if ":" in target:
                # Target is fully qualified
                callers.update(self.reverse_call_graph.get(target, set()))
            else:
                # Target is bare name: look up in symbols_by_name
                matching_syms = self.symbols_by_name.get(target, [])
                for sym in matching_syms:
                    if sym.qualified_name:
                        callers.update(
                            self.reverse_call_graph.get(sym.qualified_name, set())
                        )
                # Also check direct bare name matches if any registered
                callers.update(self.reverse_call_graph.get(target, set()))

            if callers:
                impact_map[target] = sorted(callers)

        return impact_map

    def get_symbol_definition(self, identifier: str) -> SymbolInfo | None:
        """
        Lookup definition, file location, and signature for a symbol.
        Supports qualified name or bare name.
        """
        if identifier in self.symbols:
            return self.symbols[identifier]

        matches = self.symbols_by_name.get(identifier, [])
        if matches:
            return matches[0]

        return None

    def get_symbols_by_name(self, name: str) -> list[SymbolInfo]:
        """Return all symbols matching a given name across all files in repository."""
        return self.symbols_by_name.get(name, [])

    def find_call_chains(
        self, source_func: str, sink_func: str, max_depth: int = 5
    ) -> list[list[str]]:
        """
        Find inter-procedural call chains from source_func to sink_func up to max_depth.
        Performs breadth-first path exploration across both local and cross-file qualified call graphs.
        """
        from collections import deque

        # Resolve initial source nodes
        sources: list[str] = []
        if source_func in self.call_graph or source_func in self.symbols:
            sources = [source_func]
        else:
            matching_syms = self.symbols_by_name.get(source_func, [])
            if matching_syms:
                sources = [s.qualified_name for s in matching_syms if s.qualified_name]
            else:
                for key in self.call_graph:
                    if key.endswith(f":{source_func}") or key == source_func:
                        sources.append(key)
        if not sources:
            sources = [source_func]

        sink_bare = sink_func.split(":")[-1].split(".")[-1]
        chains: list[list[str]] = []
        queue: deque[list[str]] = deque([[src] for src in sources])
        visited_paths: set[tuple[str, ...]] = set()

        while queue and len(chains) < 20:
            path = queue.popleft()
            if len(path) > max_depth + 1:
                continue

            curr = path[-1]
            curr_bare = curr.split(":")[-1].split(".")[-1]

            if len(path) >= 2 and (
                curr == sink_func
                or curr_bare == sink_bare
                or curr.endswith(f":{sink_bare}")
            ):
                chains.append(path)
                continue

            callees = set(self.call_graph.get(curr, set()))
            if not callees and curr_bare in self.call_graph:
                callees.update(self.call_graph.get(curr_bare, set()))

            for callee in sorted(callees):
                callee_bare = callee.split(":")[-1].split(".")[-1]

                if (
                    callee == sink_func
                    or callee_bare == sink_bare
                    or callee.endswith(f":{sink_bare}")
                ):
                    full_path = path + [callee]
                    if tuple(full_path) not in visited_paths:
                        visited_paths.add(tuple(full_path))
                        chains.append(full_path)
                    continue

                next_nodes: list[str] = []
                if callee in self.call_graph or callee in self.symbols:
                    next_nodes.append(callee)
                elif callee_bare in self.symbols_by_name:
                    for ms in self.symbols_by_name[callee_bare]:
                        if ms.qualified_name:
                            next_nodes.append(ms.qualified_name)
                else:
                    next_nodes.append(callee)

                for next_node in next_nodes:
                    if next_node not in path and len(path) < max_depth:
                        candidate_tuple = tuple(path + [next_node])
                        if candidate_tuple not in visited_paths:
                            visited_paths.add(candidate_tuple)
                            queue.append(path + [next_node])

        return chains

    def find_incoming_chains(
        self, sink_func: str, max_depth: int = 5
    ) -> list[list[str]]:
        """
        Find all call chains leading into sink_func (backwards traversal along reverse_call_graph).
        """
        from collections import deque

        sink_bare = sink_func.split(":")[-1].split(".")[-1]
        sink_keys = set()
        for key in self.reverse_call_graph:
            k_bare = key.split(":")[-1].split(".")[-1]
            if key == sink_func or k_bare == sink_bare or key.endswith(f":{sink_bare}"):
                sink_keys.add(key)
        if not sink_keys:
            sink_keys = {sink_func}

        chains: list[list[str]] = []
        queue: deque[list[str]] = deque([[sk] for sk in sink_keys])
        visited_paths: set[tuple[str, ...]] = set()

        while queue and len(chains) < 20:
            path = queue.popleft()
            if len(path) > max_depth + 1:
                continue

            curr = path[-1]
            curr_bare = curr.split(":")[-1].split(".")[-1]

            callers = set(self.reverse_call_graph.get(curr, set()))
            if not callers and curr_bare in self.reverse_call_graph:
                callers.update(self.reverse_call_graph.get(curr_bare, set()))

            valid_callers = [
                c
                for c in callers
                if not any(c.endswith(f":L{i}") for i in range(10))
            ]
            if not valid_callers:
                if len(path) > 1:
                    chains.append(list(reversed(path)))
                continue

            for caller in sorted(valid_callers):
                if caller not in path and len(path) < max_depth:
                    tup = tuple(path + [caller])
                    if tup not in visited_paths:
                        visited_paths.add(tup)
                        queue.append(path + [caller])

        return chains

    def format_taint_path(self, chain: list[str]) -> str:
        """
        Render a call chain or taint path into a human/agent-readable ASCII trace diagram.
        """
        if not chain:
            return ""

        lines = []
        for i, node in enumerate(chain):
            is_source = i == 0
            is_sink = i == len(chain) - 1
            sym = self.get_symbol_definition(node)
            file_loc = (
                f"{sym.file_path}:L{sym.line_start}"
                if sym
                else (node.replace(":", ":L") if ":" in node else node)
            )
            params = (
                f"({', '.join(sym.parameters)})"
                if sym and sym.parameters
                else "(...)"
            )
            node_name = (
                sym.name
                if sym
                else (node.split(":")[-1].split(".")[-1])
            )

            prefix = (
                "[SOURCE] " if is_source else ("[SINK]   " if is_sink else "         ")
            )
            lines.append(f"  {prefix}{node_name}{params} in `{file_loc}`")

            if not is_sink:
                next_node = chain[i + 1]
                next_sym = self.get_symbol_definition(next_node)
                next_name = (
                    next_sym.name
                    if next_sym
                    else (
                        next_node.split(":")[-1].split(".")[-1]
                    )
                )
                lines.append(f"            | calls `{next_name}()`")
                lines.append("            v")

        return "\n".join(lines)

    def index_diff(self, raw_diff: str):
        """
        Directly parse and index symbols, classes, functions, and call relationships
        from the added and modified code inside a unified git diff.
        """
        if not raw_diff or not raw_diff.strip():
            return

        parsed_pr = DiffParser.parse_diff(raw_diff)
        for file_diff in parsed_pr.files:
            file_path = (
                file_diff.target_file or file_diff.source_file or "modified_file.py"
            )
            rel_path = file_path.replace("\\", "/").lstrip("/")
            if rel_path.startswith(("b/", "a/")):
                rel_path = rel_path[2:]

            added_lines = [
                line_item
                for _, line_item in DiffParser.extract_added_lines_with_numbers(
                    file_diff
                )
            ]
            file_content = "\n".join(added_lines)
            if not file_content.strip():
                continue

            ext = Path(rel_path).suffix.lower()
            if ext == ".py" or not ext:
                parsed = False
                import textwrap

                for candidate in (
                    file_content,
                    textwrap.dedent(file_content),
                    f"def _dummy():\n{textwrap.indent(file_content, '    ')}",
                ):
                    try:
                        tree = ast.parse(candidate)
                        import_map = self._extract_imports(tree, rel_path)
                        self._index_ast_nodes(
                            tree, rel_path, import_map, scope_prefix=""
                        )
                        parsed = True
                        break
                    except Exception:
                        continue

                if not parsed:
                    # Regex fallback for Python diff hunks where AST cannot parse partial blocks
                    lines = file_content.splitlines()
                    for i, line in enumerate(lines, 1):
                        fn_m = re.search(
                            r"(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\((.*?)\)", line
                        )
                        if fn_m:
                            func_name = fn_m.group(1)
                            raw_params = fn_m.group(2)
                            params = [
                                p.strip().split(":")[0].split("=")[0]
                                for p in raw_params.split(",")
                                if p.strip()
                            ]
                            qualified_name = f"{rel_path}:{func_name}"
                            callees = set()
                            for next_line in lines[i : i + 30]:
                                if re.match(r"^\s*(?:def|class)\s+", next_line):
                                    break
                                call_matches = re.findall(
                                    r"([A-Za-z0-9_]+)\s*\(", next_line
                                )
                                for c in call_matches:
                                    if c not in (
                                        "if",
                                        "while",
                                        "for",
                                        "with",
                                        "print",
                                        "len",
                                        "range",
                                    ):
                                        callees.add(c)
                            sym = SymbolInfo(
                                name=func_name,
                                qualified_name=qualified_name,
                                kind="function",
                                file_path=rel_path,
                                line_start=i,
                                line_end=i,
                                docstring=None,
                                parameters=params,
                                callers=[],
                                callees=list(callees),
                            )
                            self.symbols[qualified_name] = sym
                            self.symbols_by_name.setdefault(func_name, []).append(sym)
                            self.call_graph[qualified_name] = callees
                            for callee in callees:
                                self.reverse_call_graph.setdefault(callee, set()).add(
                                    qualified_name
                                )
            else:
                self._index_non_python_file(file_content, rel_path, ext)

    def format_impact_context(self, raw_diff: str) -> str:
        """
        Extract modified functions from diff by correlating file paths and line ranges,
        and generate unambiguous cross-file caller and dependency context.
        """
        if not raw_diff or not raw_diff.strip():
            return "No cross-file caller dependencies detected in repository index."

        # Ensure symbols and calls from the diff itself are indexed
        self.index_diff(raw_diff)

        parsed_pr = DiffParser.parse_diff(raw_diff)
        target_qualified_funcs: list[str] = []

        # Correlate modified line numbers with indexed file AST symbols
        for file_diff in parsed_pr.files:
            file_path = file_diff.target_file or file_diff.source_file or "unknown"
            norm_path = file_path.replace("\\", "/").lstrip("/")
            if norm_path.startswith(("b/", "a/")):
                norm_path = norm_path[2:]
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)
            modified_line_numbers = {line_no for line_no, _ in added_lines}

            # Find symbols defined in this specific file that intersect modified lines
            file_symbols = [
                s
                for s in self.symbols.values()
                if (s.file_path == file_path or s.file_path == norm_path)
                and s.kind in ["function", "method"]
            ]

            matched_for_file = False
            for sym in file_symbols:
                if (
                    any(
                        sym.line_start <= ln <= sym.line_end
                        for ln in modified_line_numbers
                    )
                    or not modified_line_numbers
                ):
                    if sym.qualified_name:
                        target_qualified_funcs.append(sym.qualified_name)
                        matched_for_file = True

            # Fallback if diff line matching did not intersect
            if not matched_for_file:
                for line in file_diff.raw_patch.splitlines():
                    if (line.startswith(("+", "-"))) and "def " in line:
                        parts = line.split("def ", 1)[1].split("(", 1)
                        if parts:
                            func_name = parts[0].strip()
                            target_qualified_funcs.append(f"{norm_path}:{func_name}")

        impact_map = self.get_impacted_callers(target_qualified_funcs)

        lines = ["🕸️ AST Code Graph & Call Hierarchy Analysis:"]
        if impact_map:
            for target_id, callers in impact_map.items():
                target_sym = self.get_symbol_definition(target_id)
                decl_file = (
                    target_sym.file_path
                    if target_sym
                    else (target_id.split(":")[0] if ":" in target_id else "unknown")
                )
                decl_name = target_sym.name if target_sym else target_id
                lines.append(
                    f"- Function `{decl_name}` (in `{decl_file}`) is invoked by:"
                )
                for caller in callers:
                    caller_sym = self.get_symbol_definition(caller)
                    caller_file = caller_sym.file_path if caller_sym else "external"
                    caller_line = caller_sym.line_start if caller_sym else "?"
                    lines.append(f"    • `{caller}` in `{caller_file}`:L{caller_line}")
        elif target_qualified_funcs:
            lines.append(
                "No external cross-file callers detected for modified functions."
            )
            lines.append("Indexed AST functions in changeset:")
            for fn in target_qualified_funcs:
                fn_sym = self.get_symbol_definition(fn)
                if fn_sym:
                    callees_str = (
                        f" -> calls: {', '.join(fn_sym.callees)}" if fn_sym.callees else ""
                    )
                    lines.append(
                        f"  • `{fn_sym.name}` ({fn_sym.file_path}:L{fn_sym.line_start}){callees_str}"
                    )
                else:
                    lines.append(f"  • `{fn}`")
        else:
            return "No cross-file caller dependencies detected in repository index."

        return "\n".join(lines)
