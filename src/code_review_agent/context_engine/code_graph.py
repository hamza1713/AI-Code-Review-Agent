"""
AST Code Graph Indexer and Cross-File Dependency Analyzer.
Parses repository Python ASTs to build symbol tables, call graphs (callers/callees),
and cross-file impact maps for modified PR functions with qualified symbol resolution.
"""

import ast
import os
import re
import time
import threading
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple

from code_review_agent.models import SymbolInfo, CodeGraphSummary
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger

# Thread-safe global cache for indexed code graphs
_INDEX_CACHE: Dict[str, Tuple[float, Dict[str, SymbolInfo], Dict[str, List[SymbolInfo]], Dict[str, Set[str]], Dict[str, Set[str]], int]] = {}
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

    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = Path(repo_root or ".").resolve()
        # qualified_name (e.g. "path/file.py:ClassName.func") -> SymbolInfo
        self.symbols: Dict[str, SymbolInfo] = {}
        # bare_name -> List[SymbolInfo] (for multi-file lookup without collision)
        self.symbols_by_name: Dict[str, List[SymbolInfo]] = {}
        # qualified_caller -> set(callee_identifiers)
        self.call_graph: Dict[str, Set[str]] = {}
        # callee_identifier -> set(qualified_callers)
        self.reverse_call_graph: Dict[str, Set[str]] = {}
        self.indexed_files_count: int = 0

    def index_repository(self, max_files: int = 500, force_reindex: bool = False) -> CodeGraphSummary:
        """Scan and index all python source files in repository with TTL caching."""
        cache_key = f"{str(self.repo_root)}::{max_files}"
        now = time.time()

        if not force_reindex:
            with _INDEX_CACHE_LOCK:
                cached = _INDEX_CACHE.get(cache_key)
                if cached and (now - cached[0]) < INDEX_CACHE_TTL_SECONDS:
                    _, self.symbols, self.symbols_by_name, self.call_graph, self.reverse_call_graph, self.indexed_files_count = cached
                    logger.debug(f"🕸️ Code Graph Indexer: Reused cached graph for {self.repo_root} ({len(self.symbols)} symbols).")
                    return CodeGraphSummary(
                        files_indexed=self.indexed_files_count,
                        symbols_count=len(self.symbols),
                        impacted_callers={}
                    )

        self.symbols.clear()
        self.symbols_by_name.clear()
        self.call_graph.clear()
        self.reverse_call_graph.clear()
        self.indexed_files_count = 0

        supported_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java"}
        source_files: List[Path] = []
        for root, dirs, files in os.walk(self.repo_root):
            # Skip hidden and cache folders
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in ["__pycache__", "venv", ".venv", "node_modules", "build", "dist", ".git", "vendor"]
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
                self.indexed_files_count
            )

        logger.info(f"🕸️ Code Graph Indexer: Indexed {len(self.symbols)} qualified symbols across {self.indexed_files_count} files.")

        return CodeGraphSummary(
            files_indexed=self.indexed_files_count,
            symbols_count=len(self.symbols),
            impacted_callers={}
        )

    def _index_file(self, file_path: Path):
        """Parse symbols, scopes, and function calls from a file (Python, JS, TS, Go, Java)."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
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

    def _index_non_python_file(self, content: str, rel_path: str, ext: str):
        """Extract symbols and function calls from non-Python files using structured pattern matching."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            fn_name = None
            kind = "function"
            params = []

            if ext in [".js", ".jsx", ".ts", ".tsx"]:
                fn_m = re.search(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)", line)
                arrow_m = re.search(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>", line)
                class_m = re.search(r"(?:export\s+)?class\s+([A-Za-z0-9_$]+)", line)
                if fn_m:
                    fn_name = fn_m.group(1)
                    params = [p.strip().split(":")[0] for p in fn_m.group(2).split(",") if p.strip()]
                elif arrow_m:
                    fn_name = arrow_m.group(1)
                    params = [p.strip().split(":")[0] for p in arrow_m.group(2).split(",") if p.strip()]
                elif class_m:
                    fn_name = class_m.group(1)
                    kind = "class"
            elif ext == ".go":
                go_m = re.search(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\((.*?)\)", line)
                if go_m:
                    fn_name = go_m.group(1)
                    params = [p.strip().split(" ")[0] for p in go_m.group(2).split(",") if p.strip()]
            elif ext == ".java":
                java_m = re.search(r"(?:public|protected|private|static|\s)+\s+[\w\<\>\[\]]+\s+([A-Za-z0-9_]+)\s*\((.*?)\)\s*\{?", line)
                class_m = re.search(r"(?:public\s+)?class\s+([A-Za-z0-9_]+)", line)
                if java_m and java_m.group(1) not in ["if", "for", "while", "switch"]:
                    fn_name = java_m.group(1)
                    kind = "method"
                elif class_m:
                    fn_name = class_m.group(1)
                    kind = "class"

            if fn_name:
                qname = f"{rel_path}:{fn_name}"
                sym = SymbolInfo(
                    name=fn_name,
                    qualified_name=qname,
                    kind=kind,
                    file_path=rel_path,
                    line_start=i,
                    line_end=i + 10,
                    parameters=params,
                    callers=[],
                    callees=[]
                )
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(fn_name, []).append(sym)

            # Extract call expressions
            for call_m in re.finditer(r"\b([A-Za-z0-9_$]+)\s*\(", line):
                callee = call_m.group(1)
                if callee not in ["if", "for", "while", "switch", "catch", "function", "return", "import", "require", "func", "make", "new", "super", "this"]:
                    self.reverse_call_graph.setdefault(callee, set()).add(f"{rel_path}:L{i}")
                    self.reverse_call_graph.setdefault(f"{rel_path}:{callee}", set()).add(f"{rel_path}:L{i}")

    def _extract_imports(self, tree: ast.AST, rel_path: str) -> Dict[str, str]:
        """Extract explicit imports in a file to assist in qualified callee resolution."""
        imports: Dict[str, str] = {}
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
        import_map: Dict[str, str],
        scope_prefix: str = ""
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
                    callees=[]
                )
                self.symbols[class_qualified_name] = sym
                self.symbols_by_name.setdefault(class_name, []).append(sym)

                # Index methods inside class
                self._index_ast_nodes(
                    node,
                    rel_path,
                    import_map,
                    scope_prefix=f"{class_name}."
                )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                scoped_name = f"{scope_prefix}{func_name}"
                qualified_name = f"{rel_path}:{scoped_name}"
                kind = "method" if scope_prefix else "function"

                params = [arg.arg for arg in node.args.args]
                docstring = ast.get_docstring(node)

                # Extract calls inside this function
                callees: Set[str] = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        callee_target = self._resolve_callee(inner.func, rel_path, import_map)
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
                    callees=list(callees)
                )
                self.symbols[qualified_name] = sym
                self.symbols_by_name.setdefault(func_name, []).append(sym)

                self.call_graph[qualified_name] = callees
                for callee in callees:
                    self.reverse_call_graph.setdefault(callee, set()).add(qualified_name)

                # Index nested functions
                self._index_ast_nodes(
                    node,
                    rel_path,
                    import_map,
                    scope_prefix=f"{scoped_name}."
                )

    def _resolve_callee(
        self,
        node: ast.AST,
        rel_path: str,
        import_map: Dict[str, str]
    ) -> Optional[str]:
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

    def get_impacted_callers(self, target_identifiers: List[str]) -> Dict[str, List[str]]:
        """
        Identify all external functions that call any of the specified functions or symbols.
        Supports both qualified identifiers ('file.py:func') and bare names ('func').
        """
        impact_map: Dict[str, List[str]] = {}

        for target in target_identifiers:
            callers: Set[str] = set()

            if ":" in target:
                # Target is fully qualified
                callers.update(self.reverse_call_graph.get(target, set()))
            else:
                # Target is bare name: look up in symbols_by_name
                matching_syms = self.symbols_by_name.get(target, [])
                for sym in matching_syms:
                    if sym.qualified_name:
                        callers.update(self.reverse_call_graph.get(sym.qualified_name, set()))
                # Also check direct bare name matches if any registered
                callers.update(self.reverse_call_graph.get(target, set()))

            if callers:
                impact_map[target] = sorted(list(callers))

        return impact_map

    def get_symbol_definition(self, identifier: str) -> Optional[SymbolInfo]:
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

    def get_symbols_by_name(self, name: str) -> List[SymbolInfo]:
        """Return all symbols matching a given name across all files in repository."""
        return self.symbols_by_name.get(name, [])

    def format_impact_context(self, raw_diff: str) -> str:
        """
        Extract modified functions from diff by correlating file paths and line ranges,
        and generate unambiguous cross-file caller context.
        """
        if not raw_diff or not raw_diff.strip():
            return "No cross-file caller dependencies detected in repository index."

        parsed_pr = DiffParser.parse_diff(raw_diff)
        target_qualified_funcs: List[str] = []

        # Correlate modified line numbers with indexed file AST symbols
        for file_diff in parsed_pr.files:
            file_path = file_diff.target_file
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)
            modified_line_numbers = {line_no for line_no, _ in added_lines}

            # Find symbols defined in this specific file that intersect modified lines
            file_symbols = [
                s for s in self.symbols.values()
                if s.file_path == file_path and s.kind in ["function", "method"]
            ]

            matched_for_file = False
            for sym in file_symbols:
                if any(sym.line_start <= ln <= sym.line_end for ln in modified_line_numbers):
                    if sym.qualified_name:
                        target_qualified_funcs.append(sym.qualified_name)
                        matched_for_file = True

            # Fallback if diff was not in index or line matching did not intersect
            if not matched_for_file:
                for line in file_diff.raw_patch.splitlines():
                    if (line.startswith("+") or line.startswith("-")) and "def " in line:
                        parts = line.split("def ", 1)[1].split("(", 1)
                        if parts:
                            func_name = parts[0].strip()
                            target_qualified_funcs.append(f"{file_path}:{func_name}")

        impact_map = self.get_impacted_callers(target_qualified_funcs)
        if not impact_map:
            return "No cross-file caller dependencies detected in repository index."

        lines = ["🕸️ Cross-File Impact & Call Graph Analysis:"]
        for target_id, callers in impact_map.items():
            sym = self.get_symbol_definition(target_id)
            decl_file = sym.file_path if sym else (target_id.split(":")[0] if ":" in target_id else "unknown")
            decl_name = sym.name if sym else target_id
            lines.append(f"- Function `{decl_name}` (in `{decl_file}`) is invoked by:")
            for caller in callers:
                caller_sym = self.get_symbol_definition(caller)
                caller_file = caller_sym.file_path if caller_sym else "external"
                caller_line = caller_sym.line_start if caller_sym else "?"
                lines.append(f"    • `{caller}` in `{caller_file}`:L{caller_line}")

        return "\n".join(lines)
