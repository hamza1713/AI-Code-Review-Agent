"""
Codebase Context Tool for CrewAI Agents.
Provides agents with symbol lookups, call graph queries, and cross-file impact analysis.
"""

from pydantic import BaseModel, Field

from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.native_tools import BaseTool


class CodebaseContextInput(BaseModel):
    """Input parameters for CodebaseContextTool."""

    query_type: str = Field(
        ...,
        description="Type of query: 'find_callers' (find who calls a function), 'lookup_symbol' (find where a function/class is defined), 'analyze_diff_impact' (find all callers of functions in a diff), or 'find_taint_path' (trace multi-hop call chain from source to sink, e.g. 'source -> sink' or 'sink')",
    )
    target: str = Field(
        ...,
        description="Function/class name, raw diff snippet, or 'source -> sink' for taint tracing",
    )


class CodebaseContextTool(BaseTool):
    """CrewAI Tool for querying repository AST symbols and call graph dependencies."""

    name: str = "Codebase Symbol & Call Graph Context Engine"
    description: str = (
        "Query the repository-wide AST call graph to find callers of a function, "
        "look up symbol definitions across files, or analyze cross-file impact of modified code."
    )
    args_schema: type[BaseModel] = CodebaseContextInput

    _indexer: CodeGraphIndexer | None = None
    _repo_root: str | None = None

    def __init__(self, repo_root: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._repo_root = repo_root
        self._indexer = None

    def _get_indexer(self) -> CodeGraphIndexer:
        """Lazily initialize and index the repository on first use."""
        if self._indexer is None:
            self._indexer = CodeGraphIndexer(repo_root=self._repo_root)
            self._indexer.index_repository()
        return self._indexer

    def _run(self, query_type: str, target: str) -> str:
        """Execute context query against indexed codebase."""
        indexer = self._get_indexer()
        query_type = query_type.strip().lower()

        if query_type == "find_callers":
            impact = indexer.get_impacted_callers([target])
            callers = impact.get(target, [])
            if not callers:
                return f"No cross-file callers found for function '{target}' in repository."
            return f"Function '{target}' is called by: {', '.join(callers)}"

        elif query_type == "lookup_symbol":
            sym = indexer.get_symbol_definition(target)
            if not sym:
                return f"Symbol '{target}' not found in indexed codebase."
            doc = f"\n  Docstring: {sym.docstring}" if sym.docstring else ""
            return (
                f"Symbol: {sym.name} ({sym.kind})\n"
                f"  Location: `{sym.file_path}`:L{sym.line_start}-L{sym.line_end}\n"
                f"  Parameters: {', '.join(sym.parameters)}{doc}"
            )

        elif query_type == "analyze_diff_impact":
            return indexer.format_impact_context(target)

        elif query_type == "find_taint_path":
            if "->" in target:
                src, sink = [part.strip() for part in target.split("->", 1)]
            elif "," in target:
                src, sink = [part.strip() for part in target.split(",", 1)]
            else:
                src, sink = None, target.strip()

            if src:
                chains = indexer.find_call_chains(src, sink)
                if not chains:
                    return f"No inter-procedural call chains detected from '{src}' to '{sink}' (depth <= 5)."
                res = [f"🚨 Inter-Procedural Call Chain(s) from '{src}' to '{sink}':"]
                for idx, chain in enumerate(chains, 1):
                    res.append(
                        f"\nPath {idx} (hops: {len(chain) - 1}):\n"
                        + indexer.format_taint_path(chain)
                    )
                return "\n".join(res)
            else:
                chains = indexer.find_incoming_chains(sink)
                if not chains:
                    return f"No callers or upstream call chains detected leading to sink '{sink}'."
                res = [
                    f"🚨 Inter-Procedural Call Chains leading to Sink '{sink}':"
                ]
                for idx, chain in enumerate(chains, 1):
                    res.append(
                        f"\nPath {idx} (hops: {len(chain) - 1}):\n"
                        + indexer.format_taint_path(chain)
                    )
                return "\n".join(res)

        return f"Unknown query_type '{query_type}'. Use 'find_callers', 'lookup_symbol', 'analyze_diff_impact', or 'find_taint_path'."
