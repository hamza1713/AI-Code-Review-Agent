"""
Unit tests for AST Code Graph Indexer and namespace collision prevention.
Verifies that same-named functions and classes in different files are keyed by
fully-qualified paths and their caller/callee graphs remain strictly isolated.
"""

import tempfile
from pathlib import Path
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
            encoding="utf-8"
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
            encoding="utf-8"
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
