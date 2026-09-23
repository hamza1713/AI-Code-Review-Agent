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
from pathlib import Path

MAX_RESULT_BYTES = 8 * 1024 * 1024
DEFAULT_CONCURRENCY = int(os.getenv("MAX_CONCURRENT_REVIEWS", "4"))
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
    return {key: {'base64': base64.b64encode(value).decode('ascii')} if isinstance(value, bytes) else value
            for key, value in values.items()}


def decode_inputs(values):
    return {key: base64.b64decode(value['base64'], validate=True)
            if key in {'file_bytes', 'zip_bytes'} and isinstance(value, dict) else value
            for key, value in values.items()}


def stop_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def run_job(operation, payload, timeout=180.0, cancel_event=None):
    if not _slots.acquire(blocking=False):
        raise ReviewBusyError('All review workers are busy. Try again shortly.')
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='review-job-') as folder:
            root = Path(folder)
            request_file, result_file = root / 'request.json', root / 'result.json'
            request_file.write_text(json.dumps({'operation': operation, 'payload': encode_inputs(payload)}), encoding='utf-8')
            request_file.chmod(0o600)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
            env['REVIEW_WORKER_DEADLINE'] = str(time.time() + timeout)
            options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            process = subprocess.Popen([sys.executable, '-m', 'code_review_agent.review_worker',
                str(request_file), str(result_file)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **options)
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if (cancel_event and cancel_event.is_set()) or time.monotonic() >= deadline:
                        raise ReviewDeadlineError('Review deadline exceeded; the worker was stopped.')
                    if result_file.exists():
                        # The worker publishes atomically. Do not wait for third-party
                        # background threads during interpreter shutdown after work is done.
                        break
                    time.sleep(0.05)
                if not result_file.exists():
                    raise ReviewWorkerError('Review worker exited without a result.')
                if result_file.stat().st_size > MAX_RESULT_BYTES:
                    raise ReviewWorkerError('Review output exceeded its size limit.')
                result = json.loads(result_file.read_text(encoding='utf-8'))
                if 'error' in result:
                    raise ReviewWorkerError(result['error'], result.get('status', 500))
                return result['result']
            finally:
                stop_process_tree(process)
    finally:
        _slots.release()


async def run_job_async(operation, payload, timeout=180.0):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(run_job, operation, payload, timeout, cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.shield(task)
        except (ReviewDeadlineError, ReviewWorkerError):
            pass
        raise
