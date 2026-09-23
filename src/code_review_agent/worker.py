"""
Standalone background review worker process.

Enables horizontal scaling across multiple container replicas or background processes.
Workers cooperatively pull jobs from WebhookJobQueue using distributed SQLite leasing
and heartbeats with zero duplicate reviews or queue starvation.

Usage:
    python -m code_review_agent.worker
    python -m code_review_agent.worker --concurrency 4 --poll-interval 1.0
"""

import argparse
import os
import signal
import sys
import threading
import time
from code_review_agent.config import logger
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker


def run_worker():
    parser = argparse.ArgumentParser(description="AI Code Review Agent Standalone Worker")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("MAX_CONCURRENT_REVIEWS", "4")),
        help="Maximum concurrent review execution threads per worker process (default: 4 or MAX_CONCURRENT_REVIEWS)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("QUEUE_POLL_INTERVAL", "1.0")),
        help="Polling interval in seconds when queue is idle (default: 1.0)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=os.getenv("QUEUE_DB_PATH", "webhook_jobs.db"),
        help="Path to the shared SQLite queue database file",
    )
    args = parser.parse_args()

    logger.info(
        f"🚀 Initializing standalone review worker (PID {os.getpid()}) "
        f"[db: {args.db_path}, concurrency: {args.concurrency}, poll: {args.poll_interval}s]"
    )

    queue = WebhookJobQueue(db_path=args.db_path)
    worker = WebhookWorker(
        queue=queue,
        poll_interval=args.poll_interval,
        max_workers=args.concurrency,
    )

    stop_event = threading.Event()

    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"🛑 Received {sig_name}; initiating graceful worker shutdown...")
        stop_event.set()
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    worker.start()

    logger.info(f"✨ Standalone worker {worker.worker_id} active and ready for jobs.")

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        worker.stop()
        logger.info("👋 Standalone worker process terminated cleanly.")


if __name__ == "__main__":
    run_worker()
