"""
Execution Tracer and Decision Tree Visualizer.
Records multi-agent lifecycle events, routing branches, tool invocations, and timing
into an interactive hierarchical decision tree for DIY LangSmith-style trace visualization.
"""

import time
import uuid
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field

from code_review_agent.config import logger


class TraceNode(BaseModel):
    """Represents a single step or decision node in the execution trace tree."""
    id: str = Field(default_factory=lambda: f"node_{uuid.uuid4().hex[:8]}")
    title: str
    stage: str  # e.g., "INGESTION", "ROUTING", "SECURITY_SCAN", "AGENT_CREW", "GOVERNANCE", "SYNTHESIS"
    agent_name: Optional[str] = None
    status: str = "RUNNING"  # "RUNNING", "COMPLETED", "FAILED", "SKIPPED"
    started_at: float = Field(default_factory=time.time)
    ended_at: Optional[float] = None
    duration_ms: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    children: List["TraceNode"] = Field(default_factory=list)

    def complete(self, details: Optional[Dict[str, Any]] = None):
        """Mark node as completed and calculate duration."""
        self.ended_at = time.time()
        self.duration_ms = round((self.ended_at - self.started_at) * 1000, 2)
        self.status = "COMPLETED"
        if details:
            self.details.update(details)

    def fail(self, error_message: str):
        """Mark node as failed."""
        self.ended_at = time.time()
        self.duration_ms = round((self.ended_at - self.started_at) * 1000, 2)
        self.status = "FAILED"
        self.details["error"] = error_message


class ExecutionTracer:
    """
    Thread-safe execution tracer that records hierarchical agent execution graphs.
    """

    def __init__(self, trace_id: Optional[str] = None):
        self.trace_id = trace_id or f"trace_{uuid.uuid4().hex[:10]}"
        self.root_nodes: List[TraceNode] = []
        self.started_at = time.time()
        self.ended_at: Optional[float] = None

    def start_step(
        self,
        title: str,
        stage: str,
        agent_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        parent: Optional[TraceNode] = None
    ) -> TraceNode:
        """Create and start a new trace node."""
        node = TraceNode(
            title=title,
            stage=stage,
            agent_name=agent_name,
            details=details or {}
        )
        if parent:
            parent.children.append(node)
        else:
            self.root_nodes.append(node)
        return node

    def to_dict(self) -> Dict[str, Any]:
        """Export trace graph to dictionary for frontend visualization or API response."""
        total_duration = round((time.time() - self.started_at) * 1000, 2) if not self.ended_at else round((self.ended_at - self.started_at) * 1000, 2)
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "duration_ms": total_duration,
            "nodes": [node.model_dump() for node in self.root_nodes]
        }


# Global active tracer registry
_active_tracers: Dict[str, ExecutionTracer] = {}


def get_tracer(trace_id: str) -> ExecutionTracer:
    """Retrieve or create tracer by ID."""
    if trace_id not in _active_tracers:
        _active_tracers[trace_id] = ExecutionTracer(trace_id=trace_id)
    return _active_tracers[trace_id]
