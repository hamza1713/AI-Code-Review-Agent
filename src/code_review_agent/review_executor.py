"""Bounded review subprocesses. Deadline expiry terminates the process tree.

This isolates lifecycle, not untrusted code: generated code has a separate container boundary.
"""

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from code_review_agent.config import logger

MAX_RESULT_BYTES = 8 * 1024 * 1024
DEFAULT_CONCURRENCY = int(os.getenv("MAX_CONCURRENT_REVIEWS", "2"))
_slots = threading.BoundedSemaphore(DEFAULT_CONCURRENCY)


class ReviewBusyError(Exception):
    pass


class ReviewDeadlineError(Exception):
    pass


class ReviewWorkerError(Exception):
    def __init__(self, detail, status=500):
        super().__init__(detail)
        self.status = status


def encode_inputs(values):
    return {
        key: {"base64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, bytes)
        else value
        for key, value in values.items()
    }


def decode_inputs(values):
    return {
        key: base64.b64decode(value["base64"], validate=True)
        if key in {"file_bytes", "zip_bytes"} and isinstance(value, dict)
        else value
        for key, value in values.items()
    }


def stop_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


@contextmanager
def worker_workspace():
    """Give Windows time to release inherited file handles after termination.

    Cleanup must not replace the worker's result/deadline error or leak capacity.
    Any directory that remains locked is logged for operator cleanup.
    """
    directory = tempfile.TemporaryDirectory(prefix="review-job-")
    root = Path(directory.name).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("Worker workspace is outside the temporary directory")
    try:
        yield directory.name
    finally:
        for attempt in range(10):
            try:
                directory.cleanup()
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (32, 33):
                    raise
                if attempt == 9:
                    logger.warning(
                        "Stopped worker workspace remains locked: %s", directory.name
                    )
                else:
                    time.sleep(0.1)


def run_job(operation, payload, timeout=180.0, cancel_event=None, event_callback=None):
    if not _slots.acquire(blocking=False):
        raise ReviewBusyError("All review workers are busy. Try again shortly.")
    process = None
    try:
        with worker_workspace() as folder:
            root = Path(folder)
            request_file, result_file = root / "request.json", root / "result.json"
            request_file.write_text(
                json.dumps({"operation": operation, "payload": encode_inputs(payload)}),
                encoding="utf-8",
            )
            request_file.chmod(0o600)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            env["REVIEW_WORKER_DEADLINE"] = str(time.time() + timeout)
            events_file = root / "events.jsonl"
            events_pos = 0
            env["REVIEW_EVENTS_FILE"] = str(events_file)
            options = (
                {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                }
                if os.name == "nt"
                else {"start_new_session": True}
            )
            stderr_file = root / "worker_stderr.log"
            err_out = open(stderr_file, "wb")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "code_review_agent.review_worker",
                        str(request_file),
                        str(result_file),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=err_out,
                    env=env,
                    **options,
                )
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if (
                            cancel_event and cancel_event.is_set()
                        ) or time.monotonic() >= deadline:
                            raise ReviewDeadlineError(
                                "Review deadline exceeded; the worker was stopped."
                            )
                        if result_file.exists():
                            # The worker publishes atomically. Do not wait for third-party
                            # background threads during interpreter shutdown after work is done.
                            break
                        if event_callback and events_file.exists():
                            try:
                                with open(events_file, "r", encoding="utf-8") as ef:
                                    ef.seek(events_pos)
                                    new_lines = ef.read()
                                    events_pos = ef.tell()
                                    for eline in new_lines.splitlines():
                                        if eline.strip():
                                            event_callback(json.loads(eline))
                            except Exception:
                                pass
                        time.sleep(0.05)
                    if not result_file.exists():
                        stop_process_tree(process)
                        err_out.flush()
                        stderr_content = ""
                        try:
                            stderr_content = stderr_file.read_text(
                                encoding="utf-8", errors="replace"
                            ).strip()
                        except Exception:
                            pass
                        if stderr_content:
                            logger.error(
                                f"❌ Review worker subprocess crashed (exit code {process.poll()}):\n{stderr_content}"
                            )
                            summary_err = stderr_content[-1000:]
                            raise ReviewWorkerError(
                                f"Review worker crashed (code {process.poll()}): {summary_err}"
                            )
                        raise ReviewWorkerError(
                            "Review worker exited without a result."
                        )
                    if result_file.stat().st_size > MAX_RESULT_BYTES:
                        raise ReviewWorkerError(
                            "Review output exceeded its size limit."
                        )
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                    if "error" in result:
                        raise ReviewWorkerError(
                            result["error"], result.get("status", 500)
                        )
                    return result["result"]
                finally:
                    stop_process_tree(process)
            finally:
                err_out.close()
    finally:
        _slots.release()


async def run_job_async(operation, payload, timeout=180.0, event_callback=None):
    cancelled = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(run_job, operation, payload, timeout, cancelled, event_callback)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.shield(task)
        except (ReviewDeadlineError, ReviewWorkerError):
            pass
        raise
