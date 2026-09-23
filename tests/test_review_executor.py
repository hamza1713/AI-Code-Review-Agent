"""Worker deadlines are tested with real, harmless subprocesses, not LLM calls."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from code_review_agent.review_executor import run_job, ReviewBusyError, ReviewDeadlineError, encode_inputs, decode_inputs


def test_input_bytes_round_trip():
    payload = {'file_bytes': b'\x00\xff', 'raw_diff': 'abc'}
    assert decode_inputs(json.loads(json.dumps(encode_inputs(payload)))) == payload


@pytest.mark.slow
def test_timeout_terminates_worker_and_frees_capacity(tmp_path):
    original = subprocess.Popen
    spawned = []
    def sleeper(command, **options):
        if 'code_review_agent.review_worker' not in command:
            return original(command, **options)
        child = original([sys.executable, '-c', 'import time; time.sleep(60)'], **options)
        spawned.append(child)
        return child
    with patch('code_review_agent.review_executor.subprocess.Popen', sleeper):
        for _ in range(3):
            with pytest.raises(ReviewDeadlineError):
                run_job('review', {}, timeout=0.2)
            assert spawned[-1].poll() is not None


@pytest.mark.slow
def test_successful_worker_result_reaches_caller(tmp_path):
    original = subprocess.Popen
    def respond(command, **options):
        output = command[-1]
        return original([sys.executable, '-c',
            'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(\'{"result":{"ok":true}}\')', output], **options)
    with patch('code_review_agent.review_executor.subprocess.Popen', respond):
        assert run_job('review', {}, timeout=5) == {'ok': True}


def test_capacity_rejected_without_starting_extra_worker():
    from code_review_agent import review_executor
    with patch.object(review_executor, '_slots', threading.BoundedSemaphore(0)), patch('subprocess.Popen') as launch:
        with pytest.raises(ReviewBusyError): run_job('review', {})
        launch.assert_not_called()


def test_browser_job_is_durable_and_retains_kind(tmp_path):
    from code_review_agent.webhook_queue import WebhookJobQueue
    path = str(tmp_path / 'jobs.db')
    first = WebhookJobQueue(path)
    job_id = first.enqueue('Browser review', encode_inputs({'file_bytes': b'abc'}), max_retries=1, job_kind='browser')
    reopened = WebhookJobQueue(path)
    claimed = reopened.claim_next_job()
    assert claimed['job_id'] == job_id and claimed['job_kind'] == 'browser'
    assert decode_inputs(claimed['payload'])['file_bytes'] == b'abc'
    reopened.complete_job(job_id, json.dumps({'verdict': 'COMMENT'}))
    assert first.get_job(job_id)['result']['verdict'] == 'COMMENT'


def test_queue_capacity_is_bounded(tmp_path):
    from code_review_agent.webhook_queue import WebhookJobQueue
    queue = WebhookJobQueue(str(tmp_path / 'jobs.db'))
    for index in range(100): queue.enqueue(str(index), {})
    with pytest.raises(ValueError): queue.enqueue('overflow', {})
    assert len(queue.list_jobs()) == 100

@pytest.mark.slow
def test_real_worker_entrypoint_handles_read_only_help():
    result = run_job('bot', {'command_text': '/help', 'auto_post': False}, timeout=180)
    assert result['status'] == 'SUCCESS'
    assert result['command'] == 'help'

@pytest.mark.slow
def test_published_result_does_not_wait_for_background_threads():
    original = subprocess.Popen
    spawned = []
    def publish_then_wait(command, **options):
        if 'code_review_agent.review_worker' not in command:
            return original(command, **options)
        child = original([sys.executable, '-c',
            'import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); t=p.with_suffix(".tmp"); t.write_text(\'{"result":{"ok":true}}\'); t.replace(p); time.sleep(60)', command[-1]], **options)
        spawned.append(child)
        return child
    with patch('code_review_agent.review_executor.subprocess.Popen', publish_then_wait):
        assert run_job('review', {}, timeout=5) == {'ok': True}
        assert spawned[0].poll() is not None
