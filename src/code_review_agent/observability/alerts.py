"""Pure, source-free alert evaluation suitable for an authenticated metrics scraper."""


def evaluate_alerts(metrics: dict, queue_sla_seconds: float = 300) -> list[dict]:
    rules = [
        (
            not metrics.get("database_accessible", False),
            "DATABASE_UNAVAILABLE",
            "CRITICAL",
        ),
        (not metrics.get("worker_running", False), "WORKER_STOPPED", "CRITICAL"),
        (
            metrics.get("publication_reconciliation_required", 0) > 0,
            "PUBLICATION_UNCERTAIN",
            "CRITICAL",
        ),
        (
            metrics.get("queue_oldest_age_seconds", 0) > queue_sla_seconds,
            "QUEUE_LATENCY",
            "WARNING",
        ),
        (metrics.get("failed", 0) > 0, "FAILED_JOBS_REQUIRE_TRIAGE", "WARNING"),
        (
            (metrics.get("reaction_poll_lag_seconds") or 0) > 900,
            "REACTION_POLL_LAG",
            "WARNING",
        ),
    ]
    return [
        {"code": code, "severity": severity}
        for triggered, code, severity in rules
        if triggered
    ]
