"""
Unit tests for ExecutionTracer and decision tree trace node hierarchy.
"""

import pytest
from code_review_agent.observability.tracer import ExecutionTracer, TraceNode, get_tracer


class TestExecutionTracer:
    """Validate hierarchical trace tree recording and serialization."""

    def test_single_trace_node_lifecycle(self):
        node = TraceNode(title="Test Node", stage="INGESTION")
        assert node.status == "RUNNING"
        node.complete({"files": 3})
        assert node.status == "COMPLETED"
        assert node.duration_ms is not None
        assert node.details["files"] == 3

    def test_hierarchical_tracer_tree(self):
        tracer = ExecutionTracer(trace_id="test_trace_123")
        parent = tracer.start_step(title="Crew Review", stage="AGENT_CREW")
        child1 = tracer.start_step(title="Senior Dev", stage="QUALITY", parent=parent)
        child2 = tracer.start_step(title="Security Eng", stage="SECURITY", parent=parent)

        child1.complete({"status": "clean"})
        child2.complete({"status": "found_issue"})
        parent.complete()

        data = tracer.to_dict()
        assert data["trace_id"] == "test_trace_123"
        assert len(data["nodes"]) == 1
        assert len(data["nodes"][0]["children"]) == 2
        assert data["nodes"][0]["children"][0]["title"] == "Senior Dev"
