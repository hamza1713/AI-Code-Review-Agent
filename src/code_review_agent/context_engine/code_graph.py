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

        supported_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java"}
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
        elif ext in [".js", ".jsx", ".ts", ".tsx", ".go", ".java"]:
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
        """Extract symbols and function calls from non-Python files using structured pattern matching."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            fn_name = None
            kind = "function"
            params = []

            # Check if line looks like beginning of multi-line function/method
            sig_candidate = line
            if (
                any(kw in line for kw in ("function", "=>", "func", "class"))
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
