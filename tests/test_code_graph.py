"""
Unit tests for AST Code Graph Indexer and namespace collision prevention.
Verifies that same-named functions and classes in different files are keyed by
fully-qualified paths and their caller/callee graphs remain strictly isolated.
"""

import pytest

from code_review_agent.context_engine.code_graph import CodeGraphIndexer


class TestCodeGraphNamespaceIsolation:
    """Test suite ensuring cross-file symbol collision is completely eliminated."""

    @pytest.fixture
    def fixture_repo(self, tmp_path):
        """
        Creates a temporary repository structure with identical function names
        in different files and modules to test namespace collision.
        """
        # Module A
        mod_a = tmp_path / "module_a.py"
        mod_a.write_text(
            """
def process_data(payload: dict) -> bool:
    '''Process data in module A.'''
    return True

def caller_in_module_a():
    '''Calls module A's process_data.'''
    return process_data({"source": "a"})

class DataHandler:
    def handle(self):
        return process_data({})
""",
            encoding="utf-8",
        )

        # Module B
        mod_b = tmp_path / "module_b.py"
        mod_b.write_text(
            """
def process_data(payload: dict) -> bool:
    '''Process data in module B (completely different implementation).'''
    return False

def caller_in_module_b():
    '''Calls module B's process_data.'''
    return process_data({"source": "b"})

class DataHandler:
    def handle(self):
        return process_data({})
""",
            encoding="utf-8",
        )

        return tmp_path

    def test_symbols_keyed_by_qualified_name(self, fixture_repo):
        """Confirm symbols from different files do not overwrite each other."""
        indexer = CodeGraphIndexer(repo_root=str(fixture_repo))
        indexer.index_repository()

        # Both symbols should be present in qualified symbols map
        assert "module_a.py:process_data" in indexer.symbols
        assert "module_b.py:process_data" in indexer.symbols

        # Symbols by bare name should hold both definitions
        bare_matches = indexer.get_symbols_by_name("process_data")
        assert len(bare_matches) == 2
        file_paths = {s.file_path for s in bare_matches}
        assert file_paths == {"module_a.py", "module_b.py"}

    def test_call_graph_isolation_no_cross_file_conflation(self, fixture_repo):
        """
        Verify that caller_in_module_a calling process_data in module_a is NOT
        reported as a caller of process_data in module_b.
        """
        indexer = CodeGraphIndexer(repo_root=str(fixture_repo))
        indexer.index_repository()

        impact_a = indexer.get_impacted_callers(["module_a.py:process_data"])
        impact_b = indexer.get_impacted_callers(["module_b.py:process_data"])

        callers_a = impact_a.get("module_a.py:process_data", [])
        callers_b = impact_b.get("module_b.py:process_data", [])

        # module_a.py callers should only include module_a callers
        assert any("module_a.py:caller_in_module_a" in c for c in callers_a)
        assert not any("module_b.py:caller_in_module_b" in c for c in callers_a)

        # module_b.py callers should only include module_b callers
        assert any("module_b.py:caller_in_module_b" in c for c in callers_b)
        assert not any("module_a.py:caller_in_module_a" in c for c in callers_b)

    def test_class_method_qualified_names(self, fixture_repo):
        """Verify class methods are properly scoped as ClassName.method_name."""
        indexer = CodeGraphIndexer(repo_root=str(fixture_repo))
        indexer.index_repository()

        assert "module_a.py:DataHandler.handle" in indexer.symbols
        assert "module_b.py:DataHandler.handle" in indexer.symbols

        sym_a_handle = indexer.get_symbol_definition("module_a.py:DataHandler.handle")
        assert sym_a_handle is not None
        assert sym_a_handle.kind == "method"
        assert sym_a_handle.file_path == "module_a.py"

    def test_format_impact_context_with_diff(self, fixture_repo):
        """Verify format_impact_context resolves callers accurately for specific diff files."""
        indexer = CodeGraphIndexer(repo_root=str(fixture_repo))
        indexer.index_repository()

        sample_diff = """diff --git a/module_a.py b/module_a.py
--- a/module_a.py
+++ b/module_a.py
@@ -2,3 +2,3 @@
-def process_data(payload: dict) -> bool:
+def process_data(payload: dict) -> bool:
"""
        context = indexer.format_impact_context(sample_diff)
        assert "module_a.py" in context
        assert "module_a.py:caller_in_module_a" in context
        assert "module_b.py:caller_in_module_b" not in context

    def test_disk_cache_persistence_across_instances(self, fixture_repo):
        """Verify on-disk cache allows fresh instances in new memory spaces to reuse the index."""
        indexer1 = CodeGraphIndexer(repo_root=str(fixture_repo))
        summary1 = indexer1.index_repository(force_reindex=True)
        assert summary1.symbols_count > 0

        # Clear in-memory cache completely
        CodeGraphIndexer.clear_cache()

        # Create completely fresh instance (simulating a worker subprocess)
        indexer2 = CodeGraphIndexer(repo_root=str(fixture_repo))
        summary2 = indexer2.index_repository(force_reindex=False)
        assert summary2.symbols_count == summary1.symbols_count
        assert "module_a.py:process_data" in indexer2.symbols

    def test_non_python_brace_depth_tracking(self, tmp_path):
        """Verify JS/TS/Go/Java symbols compute accurate line_end via brace tracking."""
        ts_file = tmp_path / "service.ts"
        ts_content = """export const handleRequest = (req: Request, res: Response) => {
    const data = req.body;
    if (!data) {
        return res.status(400);
    }
    return res.json({ ok: true });
};
"""
        ts_file.write_text(ts_content, encoding="utf-8")

        indexer = CodeGraphIndexer(repo_root=str(tmp_path))
        indexer.index_repository(force_reindex=True)

        sym = indexer.symbols.get("service.ts:handleRequest")
        assert sym is not None
        assert sym.line_start == 1
        # The closing brace is at line 7, NOT line 11 (1 + 10)
        assert sym.line_end == 7

    def test_polyglot_ast_indexing_go_java_rust(self, tmp_path):
        """Verify Tree-Sitter AST parser extracts symbols and calls from Go, Java, and Rust files."""
        # Go file
        go_file = tmp_path / "handler.go"
        go_file.write_text(
            """package main
func ProcessData(ctx Context, query string) error {
    validate(query)
    return executeDB(query)
}
""",
            encoding="utf-8",
        )

        # Java file
        java_file = tmp_path / "OrderService.java"
        java_file.write_text(
            """public class OrderService {
    public void submitOrder(String orderId) {
        verifyOrder(orderId);
        dispatchOrder(orderId);
    }
}
""",
            encoding="utf-8",
        )

        # Rust file
        rust_file = tmp_path / "worker.rs"
        rust_file.write_text(
            """fn process_message(msg: &str) -> bool {
    decode_payload(msg);
    forward_message(msg);
    true
}
""",
            encoding="utf-8",
        )

        indexer = CodeGraphIndexer(repo_root=str(tmp_path))
        indexer.index_repository(force_reindex=True)

        # Go check
        go_sym = indexer.symbols.get("handler.go:ProcessData")
        assert go_sym is not None
        assert go_sym.kind == "function"
        assert "query" in go_sym.parameters
        assert "executeDB" in go_sym.callees

        # Java check
        java_sym = indexer.symbols.get("OrderService.java:submitOrder")
        assert java_sym is not None
        assert java_sym.kind in ("method", "function")
        assert "dispatchOrder" in java_sym.callees

        # Rust check
        rust_sym = indexer.symbols.get("worker.rs:process_message")
        assert rust_sym is not None
        assert rust_sym.kind == "function"
        assert "forward_message" in rust_sym.callees

    def test_interprocedural_call_chain_discovery(self, tmp_path):
        """Verify cross-file multi-hop call chains and taint traces are resolved end-to-end."""
        f_controller = tmp_path / "controller.py"
        f_controller.write_text(
            """from service import process_payload
def handle_webhook(request):
    raw_data = request.get_data()
    return process_payload(raw_data)
""",
            encoding="utf-8",
        )

        f_service = tmp_path / "service.py"
        f_service.write_text(
            """from db import raw_exec
def process_payload(payload):
    clean = sanitize(payload)
    return raw_exec(clean)
""",
            encoding="utf-8",
        )

        f_db = tmp_path / "db.py"
        f_db.write_text(
            """def raw_exec(sql_cmd):
    return connection.execute(sql_cmd)
""",
            encoding="utf-8",
        )

        indexer = CodeGraphIndexer(repo_root=str(tmp_path))
        indexer.index_repository(force_reindex=True)

        chains = indexer.find_call_chains(
            "handle_webhook", "raw_exec", max_depth=5
        )
        assert len(chains) >= 1
        found_chain = chains[0]
        # Chain must start with controller and terminate at raw_exec
        assert any("controller.py:handle_webhook" in node for node in found_chain)
        assert any("raw_exec" in node for node in found_chain)

        # Verify formatting
        formatted = indexer.format_taint_path(found_chain)
        assert "[SOURCE]" in formatted
        assert "[SINK]" in formatted
        assert "controller.py" in formatted

    def test_context_tool_find_taint_path(self, tmp_path):
        """Verify CodebaseContextTool executes find_taint_path queries properly."""
        from code_review_agent.context_engine.context_tool import (
            CodebaseContextTool,
        )

        f1 = tmp_path / "api.py"
        f1.write_text(
            """from core import dangerous_eval
def api_endpoint(user_input):
    return dangerous_eval(user_input)
""",
            encoding="utf-8",
        )

        f2 = tmp_path / "core.py"
        f2.write_text(
            """def dangerous_eval(expr):
    return eval(expr)
""",
            encoding="utf-8",
        )

        tool = CodebaseContextTool(repo_root=str(tmp_path))
        result = tool._run(
            query_type="find_taint_path", target="api_endpoint -> eval"
        )
        assert "🚨 Inter-Procedural Call Chain" in result
        assert "[SOURCE]" in result
        assert "[SINK]" in result
