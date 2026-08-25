"""
Unit tests for AST-driven TestGeneratorTool.
Verifies that test generation is dynamic: different function signatures produce
completely distinct test bodies, function names, parameter fixtures, and mock setups.
"""

import pytest
from code_review_agent.tools.test_generator import TestGeneratorTool


class TestTestGeneratorScaffolding:
    """Test suite ensuring test generator introspects real AST signatures."""

    def test_dynamic_scaffolding_for_different_signatures(self):
        """
        Confirm that two distinct function signatures produce completely
        different test suites referencing their respective names and parameters.
        """
        tool = TestGeneratorTool()

        # Signature 1: Tax calculation
        sig_1 = "def calculate_tax(amount: float, rate: float = 0.05) -> float:"
        test_code_1 = tool._run(function_signature=sig_1, module_path="billing.tax")

        # Signature 2: User notification
        sig_2 = "def send_notification(user_id: int, message: str, channels: list = None) -> bool:"
        test_code_2 = tool._run(function_signature=sig_2, module_path="comms.notify")

        # Confirm test_code_1 is customized for calculate_tax
        assert "from billing.tax import calculate_tax" in test_code_1
        assert "class TestCalculateTax:" in test_code_1
        assert "test_calculate_tax_happy_path" in test_code_1
        assert "amount" in test_code_1 or "10.5" in test_code_1 or "0.05" in test_code_1
        assert "send_notification" not in test_code_1

        # Confirm test_code_2 is customized for send_notification
        assert "from comms.notify import send_notification" in test_code_2
        assert "class TestSendNotification:" in test_code_2
        assert "test_send_notification_happy_path" in test_code_2
        assert "user_id" in test_code_2 or "message" in test_code_2
        assert "calculate_tax" not in test_code_2

        # Crucial: the two generated test files MUST NOT be identical
        assert test_code_1 != test_code_2

    def test_dependency_fixture_generation(self):
        """Confirm parameters like db or session trigger @pytest.fixture mock generation."""
        tool = TestGeneratorTool()
        sig = "def fetch_user_orders(db, session, user_id: int) -> list:"
        test_code = tool._run(function_signature=sig, module_path="orders.service")

        assert "mock_db" in test_code
        assert "mock_session" in test_code
        assert "@pytest.fixture" in test_code
        assert "test_fetch_user_orders_happy_path(self, mock_db, mock_session):" in test_code

    def test_async_function_support(self):
        """Confirm async functions generate async test methods with pytest.mark.asyncio."""
        tool = TestGeneratorTool()
        sig = "async def fetch_remote_metrics(endpoint: str, timeout_seconds: int = 10):"
        test_code = tool._run(function_signature=sig, module_path="infra.metrics")

        assert "@pytest.mark.asyncio" in test_code
        assert "await fetch_remote_metrics(" in test_code
