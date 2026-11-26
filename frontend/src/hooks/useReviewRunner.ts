import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch, responseError } from '../api';
import type { ReviewAPIResponse } from '../types/review';

export interface ReviewPayload {
  rawDiff?: string;
  file?: File;
  zipFile?: File;
  prUrl?: string;
}

const JOB_KEY = 'review-job-id';
const POLL_MS = 1500;
const JOB_DEADLINE_MS = 10 * 60 * 1000;

/** Builds the request body once for both the streaming and the queued path. */
function buildRequest(payload: ReviewPayload): { body: BodyInit; headers: Record<string, string> } {
  if (payload.prUrl) return { body: JSON.stringify({ pr_url: payload.prUrl }), headers: { 'Content-Type': 'application/json' } };
  if (payload.rawDiff) return { body: JSON.stringify({ raw_diff: payload.rawDiff }), headers: { 'Content-Type': 'application/json' } };
  const form = new FormData();
  if (payload.file) form.append('file', payload.file);
  else if (payload.zipFile) form.append('zip_file', payload.zipFile);
  else throw new Error('No input provided for review.');
  return { body: form, headers: {} };
}

function sleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    if (signal.aborted) abort(); else signal.addEventListener('abort', abort, { once: true });
  });
}

/** Parses one SSE event block. Multi-line `data:` fields are joined per the SSE spec. */
function parseEvent(chunk: string): { type: string; data: string } {
  let type = 'message';
  const data: string[] = [];
  for (const line of chunk.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  return { type, data: data.join('\n') };
}

class StreamFailure extends Error {}

export function useReviewRunner() {
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<ReviewAPIResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<string | null>(null);
  const [stage, setStage] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const begin = () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setIsLoading(true);
    setError(null);
    return controller;
  };

  const clearStage = () => { setStage(null); setProgress(null); };

  const waitForJob = useCallback(async (jobId: string, signal: AbortSignal) => {
    setActiveJob(jobId);
    // Only the opaque job ID is stored so a refreshed session can reconnect to it.
    sessionStorage.setItem(JOB_KEY, jobId);
    const deadline = Date.now() + JOB_DEADLINE_MS;
    while (Date.now() < deadline) {
      const response = await apiFetch(`/jobs/${encodeURIComponent(jobId)}`, { signal });
      if (!response.ok) throw new Error(await responseError(response));
      const job = await response.json();
      if (job.current_stage) setStage(job.current_stage);
      if (typeof job.stage_progress === 'number') setProgress(job.stage_progress);
      const finished = (message: string | null) => {
        sessionStorage.removeItem(JOB_KEY);
        clearStage();
        if (message) throw new Error(message);
      };
      if (job.status === 'COMPLETED') { finished(null); return job.result as ReviewAPIResponse; }
      if (job.status === 'FAILED' || job.status === 'RECONCILIATION_REQUIRED') finished(job.last_error || 'The review failed. Please try again.');
      if (job.status === 'CANCELLED') finished(job.last_error || 'The review was cancelled.');
      await sleep(POLL_MS, signal);
    }
    throw new Error('The review is still queued or running. Check the job queue for its saved result.');
  }, []);

  const streamReview = async (payload: ReviewPayload, signal: AbortSignal): Promise<ReviewAPIResponse> => {
    const { body, headers } = buildRequest(payload);
    const response = await apiFetch('/api/review/stream', { method: 'POST', signal, headers, body });
    if (!response.ok || !response.body) throw new StreamFailure('stream unavailable');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split('\n\n');
      buffer = chunks.pop() || '';
      for (const chunk of chunks) {
        const event = parseEvent(chunk);
        if (!event.data) continue;
        let parsed: Record<string, unknown>;
        try { parsed = JSON.parse(event.data); } catch { continue; }
        if (event.type === 'step') {
          const name = (parsed.current_stage || parsed.stage) as string | undefined;
          if (name) setStage(name);
          if (typeof parsed.stage_progress === 'number') setProgress(parsed.stage_progress);
        } else if (event.type === 'complete') {
          return parsed as unknown as ReviewAPIResponse;
        } else if (event.type === 'error') {
          throw new StreamFailure((parsed.error as string) || 'Review stream encountered an error.');
        }
      }
    }
    throw new StreamFailure('stream ended without a result');
  };

  const queuedReview = async (payload: ReviewPayload, signal: AbortSignal): Promise<ReviewAPIResponse> => {
    const { body, headers } = buildRequest(payload);
    const response = await apiFetch('/api/review', { method: 'POST', signal, headers: { ...headers, Prefer: 'respond-async' }, body });
    if (!response.ok) throw new Error(await responseError(response));
    const data = await response.json();
    return response.status === 202 ? waitForJob(data.job_id, signal) : data;
  };

  const submit = async (payload: ReviewPayload) => {
    setResult(null);
    const controller = begin();
    try {
      let review: ReviewAPIResponse;
      try {
        review = await streamReview(payload, controller.signal);
      } catch {
        if (controller.signal.aborted) return;
        // Any stream failure (transport, busy workers, server error event) falls
        // back to the durable queue, which retries and keeps the result.
        review = await queuedReview(payload, controller.signal);
      }
      if (!controller.signal.aborted) { setResult(review); clearStage(); }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'An unexpected error occurred during review.');
    } finally {
      if (!controller.signal.aborted) setIsLoading(false);
    }
  };

  const resume = async () => {
    const jobId = sessionStorage.getItem(JOB_KEY);
    if (!jobId) return;
    const controller = begin();
    try { setResult(await waitForJob(jobId, controller.signal)); }
    catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Unable to resume review.'); }
    finally { if (!controller.signal.aborted) setIsLoading(false); }
  };

  const cancel = () => { controllerRef.current?.abort(); setIsLoading(false); };
  const reset = () => { setResult(null); setError(null); clearStage(); };

  return {
    isLoading, result, setResult, error, setError, activeJob, stage, progress,
    submit, resume, cancel, reset,
  };
}
