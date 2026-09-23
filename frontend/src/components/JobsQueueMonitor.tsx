import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Server, Copy, ExternalLink } from 'lucide-react';
import type { WebhookJob, QueueMetrics } from '../types/review';
import { apiFetch, responseError } from '../api';

interface WebhookConfig {
  webhook_url: string;
  secret_configured: boolean;
  token_configured: boolean;
  github_connected: boolean;
  github_user: string | null;
  test_events_enabled: boolean;
}

const date = (value?: string | number | null) =>
  value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : '—';

function JobDetails({
  job,
  onClose,
  onCancel,
}: {
  job: WebhookJob;
  onClose: () => void;
  onCancel?: (jobId: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  return (
    <dialog
      ref={dialog}
      onClose={onClose}
      aria-labelledby="job-details-title"
      className="m-auto max-w-2xl w-[calc(100%-2rem)] max-h-[85vh] overflow-auto rounded-2xl border border-slate-700 bg-slate-900 p-6 text-slate-200 backdrop:bg-black/70"
    >
      <div className="flex justify-between items-center gap-4">
        <h2 id="job-details-title" className="font-semibold">
          Job details
        </h2>
        <button onClick={() => dialog.current?.close()} className="text-slate-400 hover:text-white text-sm">
          Close
        </button>
      </div>
      <dl className="my-5 grid grid-cols-[auto_1fr] gap-3 text-sm">
        <dt className="text-slate-400">ID</dt>
        <dd className="font-mono">{job.job_id}</dd>
        <dt className="text-slate-400">Source</dt>
        <dd className="break-all">{job.pr_identifier}</dd>
        <dt className="text-slate-400">Status</dt>
        <dd>
          <span className="font-semibold">{job.status}</span>
          {job.current_stage && <span className="ml-2 text-xs text-indigo-400 font-mono">({job.current_stage})</span>}
        </dd>
        {job.worker_id && (
          <>
            <dt className="text-slate-400">Worker</dt>
            <dd className="font-mono text-xs">{job.worker_id}</dd>
          </>
        )}
        {typeof job.stage_progress === 'number' && (
          <>
            <dt className="text-slate-400">Progress</dt>
            <dd>{Math.round(job.stage_progress * 100)}%</dd>
          </>
        )}
        <dt className="text-slate-400">Attempts</dt>
        <dd>
          {job.attempts || 0} / {job.max_retries || 1}
        </dd>
        <dt className="text-slate-400">Updated</dt>
        <dd>{date(job.updated_at)}</dd>
      </dl>
      {['QUEUED', 'RETRYING', 'PROCESSING'].includes(job.status) && onCancel && (
        <div className="mb-4">
          <button
            onClick={() => {
              onCancel(job.job_id);
              dialog.current?.close();
            }}
            className="rounded-lg bg-rose-600/80 hover:bg-rose-600 px-3 py-1.5 text-xs text-white font-medium"
          >
            Cancel this job
          </button>
        </div>
      )}
      {job.last_error && (
        <p role="alert" className="rounded-lg bg-rose-950 p-3 text-rose-200 text-sm mb-4">
          {job.last_error}
        </p>
      )}
      {job.result && (
        <pre className="mt-4 text-xs whitespace-pre-wrap break-all bg-slate-950 p-4 rounded-xl border border-slate-800">
          {JSON.stringify(job.result, null, 2)}
        </pre>
      )}
    </dialog>
  );
}

export function JobsQueueMonitor() {
  const [jobs, setJobs] = useState<WebhookJob[]>([]);
  const [filter, setFilter] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [selectedJob, setSelectedJob] = useState<WebhookJob | null>(null);
  const [config, setConfig] = useState<WebhookConfig | null>(null);
  const [configError, setConfigError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [simulating, setSimulating] = useState(false);
  const [metrics, setMetrics] = useState<QueueMetrics | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const refresh = useCallback(() => setRefreshVersion((value) => value + 1), []);

  const handleCancelJob = async (jobId: string) => {
    try {
      const res = await apiFetch(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
      if (!res.ok) throw new Error(await responseError(res));
      setFeedback(`Job ${jobId} cancelled.`);
      refresh();
    } catch (err) {
      setFeedback(err instanceof Error ? err.message : 'Unable to cancel job.');
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [jobsRes, metricsRes] = await Promise.all([
          apiFetch(filter === 'ALL' ? '/jobs' : `/jobs?status=${filter}`, { signal: controller.signal }),
          apiFetch('/jobs/metrics', { signal: controller.signal }).catch(() => null),
        ]);
        if (!jobsRes.ok) throw new Error(await responseError(jobsRes));
        const data = await jobsRes.json();
        if (controller.signal.aborted) return;
        setJobs(data.jobs || []);
        if (metricsRes && metricsRes.ok) {
          setMetrics(await metricsRes.json());
        }
        setUpdatedAt(Date.now());
        setError('');
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Unable to load jobs.');
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (autoRefresh) timer = setTimeout(poll, 3000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [filter, autoRefresh, refreshVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await apiFetch('/api/webhook/config', { signal: controller.signal });
        if (!response.ok) throw new Error(await responseError(response));
        const value = await response.json();
        if (!controller.signal.aborted) {
          setConfig(value);
          setConfigError('');
        }
      } catch (reason) {
        if (!controller.signal.aborted)
          setConfigError(reason instanceof Error ? reason.message : 'Unable to load webhook settings.');
      }
    })();
    return () => controller.abort();
  }, [refreshVersion]);

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-slate-700 bg-slate-900/60 p-6 space-y-4">
        <h1 className="text-2xl font-semibold">Review job queue</h1>
        <p className="text-sm text-slate-400">
          Follow browser reviews and incoming pull requests. Results stay available after you leave the page.
        </p>
        {configError && (
          <p role="alert" className="text-amber-300 text-sm">
            Webhook settings: {configError}
          </p>
        )}
        {config && (
          <>
            <div className="flex flex-wrap gap-4 text-xs">
              <span>
                {config.github_connected
                  ? `GitHub connected${config.github_user ? ` as ${config.github_user}` : ''}`
                  : 'GitHub connection unavailable'}
              </span>
              <span>
                {config.secret_configured
                  ? 'Webhook signature verification configured'
                  : 'GitHub webhook disabled: secret not configured'}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <code className="text-xs break-all flex-1">{config.webhook_url}</code>
              <button
                aria-label="Copy webhook URL"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(config.webhook_url);
                    setFeedback('Webhook URL copied.');
                  } catch {
                    setFeedback('Copy failed. Select the webhook URL and copy it manually.');
                  }
                }}
              >
                <Copy size={16} />
              </button>
            </div>
            {config.test_events_enabled && (
              <button
                disabled={simulating}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm disabled:opacity-50"
                onClick={async () => {
                  setSimulating(true);
                  setFeedback('');
                  try {
                    const response = await apiFetch('/api/webhook/test-event', { method: 'POST' });
                    if (!response.ok) throw new Error(await responseError(response));
                    setFeedback('Test event queued.');
                    refresh();
                  } catch (reason) {
                    setFeedback(reason instanceof Error ? reason.message : 'Unable to create test event.');
                  } finally {
                    setSimulating(false);
                  }
                }}
              >
                Create test event
              </button>
            )}
          </>
        )}
        {feedback && (
          <p role="status" className="text-sm text-indigo-200">
            {feedback}
          </p>
        )}
        <a
          href="https://github.com"
          target="_blank"
          rel="noreferrer"
          className="text-xs text-indigo-300 inline-flex gap-2 items-center"
        >
          Open GitHub
          <ExternalLink size={14} />
        </a>
      </section>

      {metrics && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 text-center">
            <div className="text-xs text-slate-400">Queued</div>
            <div className="text-xl font-bold text-slate-200">{metrics.queued}</div>
          </div>
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 text-center">
            <div className="text-xs text-slate-400">Processing</div>
            <div className="text-xl font-bold text-indigo-400">{metrics.processing}</div>
          </div>
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 text-center">
            <div className="text-xs text-slate-400">Active Workers</div>
            <div className="text-xl font-bold text-emerald-400">{metrics.active_workers}</div>
          </div>
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 text-center">
            <div className="text-xs text-slate-400">Avg Duration</div>
            <div className="text-xl font-bold text-slate-300">{metrics.avg_duration_seconds}s</div>
          </div>
        </div>
      )}

      <section className="rounded-2xl border border-slate-700 bg-slate-900/40 p-4 sm:p-6" aria-label="Jobs">
        <div className="flex flex-wrap justify-between items-center gap-4 mb-5">
          <h2 className="font-semibold flex items-center gap-2">
            <Server size={18} />
            Saved jobs
          </h2>
          <div className="flex gap-3 items-center">
            <button
              aria-pressed={autoRefresh}
              onClick={() => setAutoRefresh((value) => !value)}
              className="text-xs"
            >
              Auto-refresh {autoRefresh ? 'on' : 'off'}
            </button>
            <button
              aria-label="Refresh jobs"
              onClick={() => {
                setLoading(true);
                refresh();
              }}
            >
              <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 mb-4">
          {['ALL', 'QUEUED', 'PROCESSING', 'RETRYING', 'COMPLETED', 'FAILED', 'CANCELLED'].map((status) => (
            <button
              key={status}
              aria-pressed={filter === status}
              onClick={() => {
                if (status === filter) return;
                setFilter(status);
                setJobs([]);
                setUpdatedAt(null);
                setLoading(true);
                setError('');
              }}
              className={`px-3 py-2 rounded-lg text-xs ${filter === status ? 'bg-indigo-600' : 'bg-slate-800 text-slate-300'}`}
            >
              {status}
            </button>
          ))}
        </div>
        {error && (
          <div role="alert" className="bg-rose-950/40 border border-rose-700 rounded-lg p-3 mb-4 text-sm text-rose-200">
            {error} {updatedAt ? 'Showing the last successfully loaded data.' : 'No current queue data is available.'}
            <button onClick={refresh} className="underline ml-3">
              Retry
            </button>
          </div>
        )}
        <p role="status" className="text-xs text-slate-400 mb-4">
          {loading
            ? 'Loading jobs…'
            : updatedAt
            ? `Last refreshed: ${new Date(updatedAt).toLocaleTimeString()}`
            : 'Waiting for a successful refresh.'}
        </p>
        <div className="overflow-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-slate-400">
              <tr>
                {['Source', 'Status / Stage', 'Attempts', 'Created', 'Actions'].map((label) => (
                  <th scope="col" key={label} className="p-3">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id} className="border-t border-slate-800">
                  <td className="p-3 break-all font-mono text-xs">{job.pr_identifier}</td>
                  <td className="p-3 text-xs">
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-[11px] font-medium ${
                        job.status === 'COMPLETED'
                          ? 'bg-emerald-950 text-emerald-300'
                          : job.status === 'FAILED'
                          ? 'bg-rose-950 text-rose-300'
                          : job.status === 'CANCELLED'
                          ? 'bg-slate-800 text-slate-400'
                          : job.status === 'PROCESSING'
                          ? 'bg-indigo-950 text-indigo-300'
                          : 'bg-amber-950 text-amber-300'
                      }`}
                    >
                      {job.status}
                    </span>
                    {job.current_stage && job.status === 'PROCESSING' && (
                      <div className="text-[11px] text-indigo-400 mt-1 font-mono">
                        {job.current_stage}{' '}
                        {typeof job.stage_progress === 'number' &&
                          `(${Math.round(job.stage_progress * 100)}%)`}
                      </div>
                    )}
                  </td>
                  <td className="p-3 text-xs">
                    {job.attempts || 0} / {job.max_retries || 1}
                  </td>
                  <td className="p-3 text-xs whitespace-nowrap">{date(job.created_at)}</td>
                  <td className="p-3 text-xs space-x-2">
                    <button
                      aria-label={`Inspect ${job.job_id}`}
                      onClick={() => setSelectedJob(job)}
                      className="text-indigo-300 hover:underline"
                    >
                      Inspect
                    </button>
                    {['QUEUED', 'RETRYING', 'PROCESSING'].includes(job.status) && (
                      <button onClick={() => handleCancelJob(job.job_id)} className="text-rose-400 hover:underline">
                        Cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {!loading && !error && !jobs.length && (
                <tr>
                  <td colSpan={5} className="p-8 text-center text-slate-400">
                    No jobs match this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
      {selectedJob && (
        <JobDetails
          job={jobs.find((job) => job.job_id === selectedJob.job_id) || selectedJob}
          onClose={() => setSelectedJob(null)}
          onCancel={handleCancelJob}
        />
      )}
    </div>
  );
}
