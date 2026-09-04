"""
Sandbox execution environment for running AI-generated tests safely against PR diffs.
"""

from .test_runner import SandboxTestRunner

__all__ = ["SandboxTestRunner"]
