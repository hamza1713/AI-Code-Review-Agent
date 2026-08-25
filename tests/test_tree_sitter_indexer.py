"""
Unit tests for MultiLanguageCodeGraphIndexer.
Verifies symbol extraction and cross-file caller resolution for Python, TypeScript/JavaScript, and Go.
"""

import pytest
from pathlib import Path
from code_review_agent.context_engine.tree_sitter_indexer import MultiLanguageCodeGraphIndexer


class TestMultiLanguageCodeGraphIndexer:
    """Test suite for multi-language AST indexing."""

    @pytest.fixture
    def multi_lang_repo(self, tmp_path):
        """Create a temporary repository with Python, TypeScript, and Go files."""
        # TypeScript file
        ts_file = tmp_path / "authService.ts"
        ts_file.write_text("""
export async function authenticateUser(token: string): Promise<boolean> {
    return token.length > 0;
}

export function handleLoginRequest(req: any) {
    return authenticateUser(req.token);
}
""", encoding="utf-8")

        # Go file
        go_file = tmp_path / "payment.go"
        go_file.write_text("""
package main

func ProcessPayment(amount float64) bool {
    return amount > 0
}

func ExecuteCheckout() {
    ProcessPayment(100.0)
}
""", encoding="utf-8")

        # Python file
        py_file = tmp_path / "utils.py"
        py_file.write_text("""
def helper_func(x: int) -> int:
    return x * 2

def main():
    return helper_func(10)
""", encoding="utf-8")

        return tmp_path

    def test_multi_lang_symbol_indexing(self, multi_lang_repo):
        """Verify symbols are indexed from TS, Go, and Python files."""
        indexer = MultiLanguageCodeGraphIndexer(repo_root=str(multi_lang_repo))
        summary = indexer.index_repository()

        assert summary.files_indexed == 3
        # Check TS symbols
        assert "authService.ts:authenticateUser" in indexer.symbols
        assert "authService.ts:handleLoginRequest" in indexer.symbols

        # Check Go symbols
        assert "payment.go:ProcessPayment" in indexer.symbols
        assert "payment.go:ExecuteCheckout" in indexer.symbols

        # Check Python symbols
        assert "utils.py:helper_func" in indexer.symbols

    def test_multi_lang_caller_resolution(self, multi_lang_repo):
        """Verify cross-file caller detection works across TS and Go."""
        indexer = MultiLanguageCodeGraphIndexer(repo_root=str(multi_lang_repo))
        indexer.index_repository()

        # Check TS caller
        ts_callers = indexer.get_impacted_callers(["authenticateUser"])
        assert "authenticateUser" in ts_callers
        assert any("authService.ts" in c for c in ts_callers["authenticateUser"])

        # Check Go caller
        go_callers = indexer.get_impacted_callers(["ProcessPayment"])
        assert "ProcessPayment" in go_callers
        assert any("payment.go" in c for c in go_callers["ProcessPayment"])
