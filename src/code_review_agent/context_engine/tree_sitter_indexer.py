"""
Multi-Language AST Symbol & Call Graph Indexer.
Supports Python, JavaScript, TypeScript, Go, and Java symbol parsing,
scope resolution, and cross-file caller tracking.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple, Any

from code_review_agent.models import SymbolInfo, CodeGraphSummary
from code_review_agent.config import logger


class MultiLanguageCodeGraphIndexer:
    """
    Multi-language symbol indexer and call graph engine.
    Supports Python, JavaScript, TypeScript, Go, and Java files.
    """

    SUPPORTED_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".java": "java",
    }

    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = Path(repo_root or ".").resolve()
        self.symbols: Dict[str, SymbolInfo] = {}
        self.symbols_by_name: Dict[str, List[SymbolInfo]] = {}
        self.call_graph: Dict[str, Set[str]] = {}
        self.reverse_call_graph: Dict[str, Set[str]] = {}
        self.indexed_files_count: int = 0

    def index_repository(self, max_files: int = 1000) -> CodeGraphSummary:
        """Scan and index all supported source files in the repository."""
        self.symbols.clear()
        self.symbols_by_name.clear()
        self.call_graph.clear()
        self.reverse_call_graph.clear()
        self.indexed_files_count = 0

        target_files: List[Path] = []
        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in ["__pycache__", "venv", ".venv", "node_modules", "build", "dist", ".git", "vendor"]
            ]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext in self.SUPPORTED_EXTENSIONS:
                    target_files.append(Path(root) / f)
                    if len(target_files) >= max_files:
                        break

        for file_path in target_files:
            self._index_file(file_path)

        self.indexed_files_count = len(target_files)
        logger.info(
            f"🕸️ Multi-Language Code Graph: Indexed {len(self.symbols)} symbols across {self.indexed_files_count} files."
        )

        return CodeGraphSummary(
            files_indexed=self.indexed_files_count,
            symbols_count=len(self.symbols),
            impacted_callers={}
        )

    def _index_file(self, file_path: Path):
        """Index a single file based on its extension."""
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
        lang = self.SUPPORTED_EXTENSIONS.get(ext)

        if lang == "python":
            self._index_python(content, rel_path)
        elif lang in ["javascript", "typescript"]:
            self._index_js_ts(content, rel_path)
        elif lang == "go":
            self._index_go(content, rel_path)
        elif lang == "java":
            self._index_java(content, rel_path)

    def _index_python(self, content: str, rel_path: str):
        """Index Python file using native ast."""
        import ast
        try:
            tree = ast.parse(content, filename=rel_path)
        except Exception:
            # Fallback to regex parser if AST fails
            self._index_python_fallback(content, rel_path)
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_name = node.name
                qname = f"{rel_path}:{class_name}"
                sym = SymbolInfo(
                    name=class_name,
                    qualified_name=qname,
                    kind="class",
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    docstring=ast.get_docstring(node),
                    parameters=[],
                    callers=[],
                    callees=[]
                )
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(class_name, []).append(sym)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                qname = f"{rel_path}:{func_name}"
                params = [arg.arg for arg in node.args.args if arg.arg not in ["self", "cls"]]

                # Find callees in this function
                callees: Set[str] = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        if isinstance(inner.func, ast.Name):
                            callees.add(inner.func.id)
                        elif isinstance(inner.func, ast.Attribute):
                            callees.add(inner.func.attr)

                sym = SymbolInfo(
                    name=func_name,
                    qualified_name=qname,
                    kind="function",
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    docstring=ast.get_docstring(node),
                    parameters=params,
                    callers=[],
                    callees=list(callees)
                )
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(func_name, []).append(sym)

                self.call_graph[qname] = callees
                for callee in callees:
                    self.reverse_call_graph.setdefault(callee, set()).add(qname)
                    self.reverse_call_graph.setdefault(f"{rel_path}:{callee}", set()).add(qname)

    def _index_python_fallback(self, content: str, rel_path: str):
        """Fallback regex parser for Python."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            func_match = re.match(r"^\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\((.*?)\)", line)
            if func_match:
                fname = func_match.group(1)
                qname = f"{rel_path}:{fname}"
                sym = SymbolInfo(
                    name=fname,
                    qualified_name=qname,
                    kind="function",
                    file_path=rel_path,
                    line_start=i,
                    line_end=i + 5,
                    parameters=[p.strip().split(":")[0] for p in func_match.group(2).split(",") if p.strip()],
                    callers=[],
                    callees=[]
                )
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(fname, []).append(sym)

    def _index_js_ts(self, content: str, rel_path: str):
        """Index JavaScript and TypeScript files."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            # Function declaration: function foo(...)
            fn_match = re.search(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)", line)
            # Arrow/const function: const foo = (async)? (...) =>
            arrow_match = re.search(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>", line)
            # Class declaration: class Foo
            class_match = re.search(r"(?:export\s+)?class\s+([A-Za-z0-9_$]+)", line)

            if fn_match:
                name = fn_match.group(1)
                qname = f"{rel_path}:{name}"
                params = [p.strip().split(":")[0] for p in fn_match.group(2).split(",") if p.strip()]
                sym = SymbolInfo(name=name, qualified_name=qname, kind="function", file_path=rel_path, line_start=i, line_end=i+10, parameters=params)
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(name, []).append(sym)
            elif arrow_match:
                name = arrow_match.group(1)
                qname = f"{rel_path}:{name}"
                params = [p.strip().split(":")[0] for p in arrow_match.group(2).split(",") if p.strip()]
                sym = SymbolInfo(name=name, qualified_name=qname, kind="function", file_path=rel_path, line_start=i, line_end=i+10, parameters=params)
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(name, []).append(sym)
            elif class_match:
                name = class_match.group(1)
                qname = f"{rel_path}:{name}"
                sym = SymbolInfo(name=name, qualified_name=qname, kind="class", file_path=rel_path, line_start=i, line_end=i+20, parameters=[])
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(name, []).append(sym)

            # Look for calls: foo(...)
            for call_match in re.finditer(r"\b([A-Za-z0-9_$]+)\s*\(", line):
                callee = call_match.group(1)
                if callee not in ["if", "for", "while", "switch", "catch", "function", "return", "import", "require"]:
                    self.reverse_call_graph.setdefault(callee, set()).add(f"{rel_path}:L{i}")

    def _index_go(self, content: str, rel_path: str):
        """Index Go source files."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            # func Foo(...) or func (r *Receiver) Foo(...)
            fn_match = re.search(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\((.*?)\)", line)
            if fn_match:
                name = fn_match.group(1)
                qname = f"{rel_path}:{name}"
                params = [p.strip().split(" ")[0] for p in fn_match.group(2).split(",") if p.strip()]
                sym = SymbolInfo(name=name, qualified_name=qname, kind="function", file_path=rel_path, line_start=i, line_end=i+15, parameters=params)
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(name, []).append(sym)

            for call_match in re.finditer(r"\b([A-Za-z0-9_]+)\s*\(", line):
                callee = call_match.group(1)
                if callee not in ["func", "if", "for", "switch", "return", "make", "new", "append", "len", "cap", "panic"]:
                    self.reverse_call_graph.setdefault(callee, set()).add(f"{rel_path}:L{i}")

    def _index_java(self, content: str, rel_path: str):
        """Index Java source files."""
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            method_match = re.search(r"(?:public|protected|private|static|\s)+\s+[\w\<\>\[\]]+\s+([A-Za-z0-9_]+)\s*\((.*?)\)\s*\{?", line)
            class_match = re.search(r"(?:public\s+)?class\s+([A-Za-z0-9_]+)", line)

            if method_match:
                name = method_match.group(1)
                if name not in ["if", "for", "while", "switch"]:
                    qname = f"{rel_path}:{name}"
                    sym = SymbolInfo(name=name, qualified_name=qname, kind="method", file_path=rel_path, line_start=i, line_end=i+15, parameters=[])
                    self.symbols[qname] = sym
                    self.symbols_by_name.setdefault(name, []).append(sym)
            elif class_match:
                name = class_match.group(1)
                qname = f"{rel_path}:{name}"
                sym = SymbolInfo(name=name, qualified_name=qname, kind="class", file_path=rel_path, line_start=i, line_end=i+30, parameters=[])
                self.symbols[qname] = sym
                self.symbols_by_name.setdefault(name, []).append(sym)

            for call_match in re.finditer(r"\b([A-Za-z0-9_]+)\s*\(", line):
                callee = call_match.group(1)
                if callee not in ["if", "for", "while", "switch", "super", "this", "new", "return"]:
                    self.reverse_call_graph.setdefault(callee, set()).add(f"{rel_path}:L{i}")

    def get_impacted_callers(self, target_identifiers: List[str]) -> Dict[str, List[str]]:
        """Find external callers of specified functions/symbols."""
        impact_map: Dict[str, List[str]] = {}
        for target in target_identifiers:
            callers: Set[str] = set()
            if ":" in target:
                callers.update(self.reverse_call_graph.get(target, set()))
                bare = target.split(":")[-1]
                callers.update(self.reverse_call_graph.get(bare, set()))
            else:
                matches = self.symbols_by_name.get(target, [])
                for sym in matches:
                    if sym.qualified_name:
                        callers.update(self.reverse_call_graph.get(sym.qualified_name, set()))
                callers.update(self.reverse_call_graph.get(target, set()))

            if callers:
                impact_map[target] = sorted(list(callers))
        return impact_map

    def get_symbol_definition(self, identifier: str) -> Optional[SymbolInfo]:
        """Lookup definition and file location for a symbol."""
        if identifier in self.symbols:
            return self.symbols[identifier]
        matches = self.symbols_by_name.get(identifier, [])
        return matches[0] if matches else None
