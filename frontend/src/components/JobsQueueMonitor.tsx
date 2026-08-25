import React, { useState, useEffect } from 'react';
import { Server, RefreshCw, GitPullRequest } from 'lucide-react';
import type { WebhookJob } from '../types/review';

export const JobsQueueMonitor: React.FC = () => {
  const [jobs, setJobs] = useState<WebhookJob[]>([]);
  const [filterStatus, setFilterStatus] = useState<string>('ALL');
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedJob, setSelectedJob] = useState<WebhookJob | null>(null);

  const fetchJobs = async () => {
    setLoading(true);
    try {
      const url = filterStatus === 'ALL' ? '/jobs' : `/jobs?status=${filterStatus}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setJobs(data.jobs || []);
      }
    } catch (err) {
      console.error('Error fetching jobs:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchJobs();
    let interval: ReturnType<typeof setInterval> | null = null;
    if (autoRefresh) {
      interval = setInterval(fetchJobs, 4000);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [filterStatus, autoRefresh]);

  const getStatusBadge = (status: string) => {
    const s = (status || '').toUpperCase();
    if (s === 'COMPLETED') {
      return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
    }
    if (s === 'PROCESSING') {
      return 'bg-indigo-500/20 text-indigo-300 border-indigo-500/40 animate-pulse';
    }
    if (s === 'QUEUED') {
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    }
    if (s === 'FAILED') {
      return 'bg-rose-500/20 text-rose-300 border-rose-500/40';
    }
    return 'bg-slate-800 text-slate-400 border-slate-700';
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-2xl p-6 shadow-2xl space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 pb-6 border-b border-slate-800/80">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
            <Server className="w-5 h-5" />
          </div>
          <div>
            <h2 className="text-lg font-bold text-white tracking-tight flex items-center gap-2">
              <span>Durable Webhook Task Queue Monitor</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-300 border border-purple-500/20 font-mono">
                SQLite ACID
              </span>
            </h2>
            <p className="text-xs text-slate-400">
              Persistent background queue with crash recovery and exponential backoff workers
            </p>
          </div>
        </div>

        {/* Actions & Filters */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Status filters */}
          <div className="flex items-center bg-slate-900 p-1 rounded-xl border border-slate-800 text-xs">
            {['ALL', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED'].map((st) => (
              <button
                key={st}
                onClick={() => setFilterStatus(st)}
                className={`px-2.5 py-1 rounded-lg font-semibold transition-all ${
                  filterStatus === st
                    ? 'bg-purple-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {st}
              </button>
            ))}
          </div>

          {/* Auto-refresh toggle */}
          <button
            onClick={() => setAutoRefresh(!autoRefresh)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl border text-xs font-semibold transition-all ${
              autoRefresh
                ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
                : 'bg-slate-900 border-slate-800 text-slate-400'
            }`}
          >
            <span className={`w-2 h-2 rounded-full ${autoRefresh ? 'bg-emerald-400 animate-ping' : 'bg-slate-500'}`} />
            <span>Auto-Refresh</span>
          </button>

          {/* Refresh button */}
          <button
            onClick={fetchJobs}
            disabled={loading}
            className="p-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 transition-colors"
            title="Refresh jobs"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin text-purple-400' : ''}`} />
          </button>
        </div>
      </div>

      {/* Jobs Table */}
      <div className="rounded-xl border border-slate-800 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-900/90 text-slate-400 uppercase font-mono tracking-wider border-b border-slate-800 text-[10px]">
              <tr>
                <th className="py-3 px-4">Job ID</th>
                <th className="py-3 px-4">PR Identifier</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Retries</th>
                <th className="py-3 px-4">Created</th>
                <th className="py-3 px-4">Completed</th>
                <th className="py-3 px-4 text-right">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono text-slate-300">
              {jobs.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-center py-8 text-slate-500 font-sans">
                    No background webhook jobs found. Incoming GitHub webhooks will durably appear here.
                  </td>
                </tr>
              ) : (
                jobs.map((job) => (
                  <tr key={job.job_id} className="hover:bg-slate-900/40 transition-colors">
                    <td className="py-3 px-4 font-bold text-indigo-400">{job.job_id.slice(0, 12)}...</td>
                    <td className="py-3 px-4 font-sans font-medium text-slate-200">
                      <div className="flex items-center gap-1.5">
                        <GitPullRequest className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                        <span>{job.pr_identifier || 'In-Memory Review'}</span>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${getStatusBadge(job.status)}`}>
                        {job.status}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-slate-400">{job.retry_count || 0}</td>
                    <td className="py-3 px-4 text-slate-400 text-[11px]">
                      {job.created_at ? new Date(job.created_at).toLocaleTimeString() : '-'}
                    </td>
                    <td className="py-3 px-4 text-slate-400 text-[11px]">
                      {job.completed_at ? new Date(job.completed_at).toLocaleTimeString() : '-'}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <button
                        onClick={() => setSelectedJob(job)}
                        className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 font-sans text-[11px] transition-colors"
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Selected Job Details Modal */}
      {selectedJob && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#12151c] border border-slate-800 rounded-2xl max-w-2xl w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between pb-3 border-b border-slate-800">
              <h3 className="text-sm font-bold text-white flex items-center gap-2">
                <span>Job Details:</span>
                <span className="font-mono text-purple-400">{selectedJob.job_id}</span>
              </h3>
              <button
                onClick={() => setSelectedJob(null)}
                className="text-slate-400 hover:text-white text-xs px-2 py-1 rounded bg-slate-800"
              >
                ✕ Close
              </button>
            </div>

            <div className="space-y-2 text-xs">
              <div className="grid grid-cols-2 gap-2 text-slate-300">
                <div><strong>PR:</strong> {selectedJob.pr_identifier}</div>
                <div><strong>Status:</strong> {selectedJob.status}</div>
                <div><strong>Created:</strong> {selectedJob.created_at}</div>
                <div><strong>Completed:</strong> {selectedJob.completed_at || 'In progress / Pending'}</div>
              </div>

              {selectedJob.error_message && (
                <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/20 text-rose-300">
                  <strong>Error:</strong> {selectedJob.error_message}
                </div>
              )}

              {selectedJob.result && (
                <div className="space-y-1">
                  <span className="font-semibold text-slate-400">Result Payload:</span>
                  <pre className="p-3 rounded-lg bg-[#0a0c10] border border-slate-800 text-[11px] font-mono text-emerald-300 max-h-60 overflow-y-auto">
                    {JSON.stringify(selectedJob.result, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
