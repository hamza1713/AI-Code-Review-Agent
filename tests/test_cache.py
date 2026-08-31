"""
Unit tests for ContentHashCache and memoization cost optimizer.
"""

import pytest
from code_review_agent.cache import ContentHashCache, memoize_by_content


class TestCacheAndCostOptimizer:
    """Validate hashing, caching, and token savings metrics."""

    def test_hash_key_deterministic(self):
        k1 = ContentHashCache.hash_key("def test(): pass", "ns1")
        k2 = ContentHashCache.hash_key("def test(): pass", "ns1")
        k3 = ContentHashCache.hash_key("def test(): pass", "ns2")

        assert k1 == k2
        assert k1 != k3

    def test_cache_hit_and_miss_lifecycle(self):
        cache = ContentHashCache(capacity=10)
        assert cache.get("nonexistent") is None
        assert cache.stats.misses == 1

        cache.set("key1", {"result": "ok"}, estimated_tokens=100)
        val = cache.get("key1")
        assert val == {"result": "ok"}
        assert cache.stats.hits == 1
        assert cache.stats.saved_operations == 1
        assert cache.stats.estimated_tokens_saved == 100

    def test_memoize_decorator(self):
        calls = {"count": 0}

        @memoize_by_content("test_func")
        def expensive_operation(content: str):
            calls["count"] += 1
            return f"Processed: {content}"

        res1 = expensive_operation("sample code")
        res2 = expensive_operation("sample code")

        assert res1 == "Processed: sample code"
        assert res2 == "Processed: sample code"
        assert calls["count"] == 1  # Second call served from cache
