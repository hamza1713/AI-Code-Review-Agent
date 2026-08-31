import React, { useState, useEffect } from 'react';
import {
  Server,
  RefreshCw,
  GitPullRequest,
  Copy,
  Check,
  Sparkles,
  ExternalLink,
  ShieldCheck,
  AlertCircle,
  Play,
  Key,
  CheckCircle2
} from 'lucide-react';
import type { WebhookJob } from '../types/review';

interface WebhookConfig {
  webhook_url: string;
  secret_configured: boolean;
  token_configured: boolean;
  github_connected: boolean;
  github_user: string | null;
}

export const JobsQueueMonitor: React.FC = () => {
  const [jobs, setJobs] = useState<WebhookJob[]>([]);
  const [filterStatus, setFilterStatus] = useState<string>('ALL');
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedJob, setSelectedJob] = useState<WebhookJob | null>(null);
  const [config, setConfig] = useState<WebhookConfig | null>(null);
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [isSimulating, setIsSimulating] = useState(false);
  const [simFeedback, setSimFeedback] = useState<string | null>(null);

  const fetchConfig = async () => {
    try {
      const res = await fetch('/api/webhook/config');
      if (res.ok) {
        const data = await res.json();
        setConfig(data);
      }
    } catch (err) {
      console.error('Error fetching webhook config:', err);
    }
  };

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
    fetchConfig();
    fetchJobs();
    let interval: ReturnType<typeof setInterval> | null = null;
    if (autoRefresh) {
      interval = setInterval(fetchJobs, 3000);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [filterStatus, autoRefresh]);

  const handleCopy = (text: string, field: string) => {
    navigator.clipboard.writeText(text);
    setCopiedField(field);
    setTimeout(() => setCopiedField(null), 2000);
  };

  const handleTriggerSimulatedWebhook = async () => {
    setIsSimulating(true);
    setSimFeedback(null);
    try {
      const res = await fetch('/api/webhook/test-event', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        setSimFeedback(`✅ ${data.message}`);
        fetchJobs();
      } else {
        setSimFeedback('❌ Failed to trigger test event.');
      }
    } catch {
      setSimFeedback('❌ Network error triggering test event.');
    } finally {
      setIsSimulating(false);
      setTimeout(() => setSimFeedback(null), 5000);
    }
  };

  const formatTimestamp = (val?: string | number | null) => {
    if (!val) return '-';
    if (typeof val === 'number') {
      return new Date(val * 1000).toLocaleTimeString();
    }
    return new Date(val).toLocaleTimeString();
  };

  const formatDateTime = (val?: string | number | null) => {
    if (!val) return '-';
    if (typeof val === 'number') {
      return new Date(val * 1000).toLocaleString();
    }
    return new Date(val).toLocaleString();
  };

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


  const currentWebhookUrl = config?.webhook_url || `${window.location.origin}/webhook/github`;

  return (
    <div className="space-y-6">
      {/* 1. Interactive Live GitHub Webhook Setup Card */}
      <div className="bg-gradient-to-br from-[#121624] via-[#101420] to-[#0c0f18] border border-indigo-500/30 rounded-2xl p-5 sm:p-6 shadow-2xl space-y-5">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 pb-4 border-b border-slate-800/80">
          <div className="flex items-center space-x-3.5">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shadow-inner">
              <Sparkles className="w-5 h-5 text-amber-400" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-white tracking-tight">
                  GitHub Live Webhook Setup & Testing Hub
                </h2>
                <span className="text-[10px] font-mono bg-emerald-500/10 text-emerald-300 border border-emerald-500/30 px-2 py-0.5 rounded-full">
                  Automated PR Ingestion
                </span>
              </div>
              <p className="text-xs text-slate-400 mt-0.5">
                Connect your GitHub repository to automatically run multi-agent reviews and post inline fixes on every Pull Request.
              </p>
            </div>
          </div>

          {/* Status Chips */}
          <div className="flex flex-wrap items-center gap-2">
            {config?.github_connected ? (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/30 font-medium">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>GitHub API Connected ({config.github_user ? `@${config.github_user}` : 'Active'})</span>
              </span>
            ) : config?.token_configured ? (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs bg-amber-500/10 text-amber-300 border border-amber-500/30 font-medium">
                <AlertCircle className="w-3.5 h-3.5" />
                <span>GITHUB_TOKEN loaded (unverified)</span>
              </span>
            ) : (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs bg-slate-800 text-slate-400 border border-slate-700 font-medium">
                <Key className="w-3.5 h-3.5 text-amber-400" />
                <span>GITHUB_TOKEN missing in .env</span>
              </span>
            )}

            {config?.secret_configured ? (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs bg-indigo-500/10 text-indigo-300 border border-indigo-500/30 font-medium">
                <ShieldCheck className="w-3.5 h-3.5" />
                <span>HMAC Secret Active</span>
              </span>
            ) : (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs bg-amber-500/10 text-amber-300 border border-amber-500/30 font-medium">
                <AlertCircle className="w-3.5 h-3.5" />
                <span>WEBHOOK_SECRET missing</span>
              </span>
            )}
          </div>
        </div>

        {/* Setup Parameters Table */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs font-mono">
          {/* Payload URL */}
          <div className="bg-[#0a0d14] p-3 rounded-xl border border-slate-800 space-y-1.5">
            <div className="flex items-center justify-between text-slate-400 font-sans text-[11px]">
              <span className="font-semibold text-slate-300">1. Webhook Payload URL</span>
              <button
                onClick={() => handleCopy(currentWebhookUrl, 'url')}
                className="text-indigo-400 hover:text-indigo-200 flex items-center gap-1 text-[10px] cursor-pointer"
              >
                {copiedField === 'url' ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                <span>{copiedField === 'url' ? 'Copied' : 'Copy'}</span>
              </button>
            </div>
            <div className="bg-slate-900/80 px-2.5 py-1.5 rounded text-indigo-300 overflow-x-auto text-[11px]">
              {currentWebhookUrl}
            </div>
          </div>

          {/* Content Type & Events */}
          <div className="bg-[#0a0d14] p-3 rounded-xl border border-slate-800 space-y-1.5">
            <div className="flex items-center justify-between text-slate-400 font-sans text-[11px]">
              <span className="font-semibold text-slate-300">2. Configuration Settings</span>
              <span className="text-[10px] text-slate-500">GitHub Webhook Form</span>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[11px]">
              <div className="bg-slate-900/80 px-2 py-1 rounded text-slate-300">
                <span className="text-slate-500 block text-[9px] font-sans">Content type:</span>
                application/json
              </div>
              <div className="bg-slate-900/80 px-2 py-1 rounded text-emerald-300">
                <span className="text-slate-500 block text-[9px] font-sans">Trigger event:</span>
                Pull requests
              </div>
            </div>
          </div>
        </div>

        {/* Action Buttons: Live Simulator & Direct Link */}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <button
              onClick={handleTriggerSimulatedWebhook}
              disabled={isSimulating}
              className="flex items-center gap-2 px-4 py-2 rounded-xl bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white text-xs font-bold shadow-md shadow-indigo-500/20 transition-all cursor-pointer disabled:opacity-50"
            >
              <Play className={`w-3.5 h-3.5 ${isSimulating ? 'animate-spin' : ''}`} />
              <span>Simulate Inbound PR Webhook</span>
            </button>

            <a
              href="https://github.com"
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 text-xs font-semibold transition-colors"
            >
              <span>Open GitHub</span>
              <ExternalLink className="w-3.5 h-3.5 text-slate-400" />
            </a>
          </div>

          {simFeedback && (
            <span className="text-xs font-medium text-emerald-400">
              {simFeedback}
            </span>
          )}
        </div>
      </div>

      {/* 2. Durable Task Queue Monitor Table */}
      <div className="bg-[#12151c] border border-slate-800/80 rounded-2xl p-6 shadow-2xl space-y-6">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-4 pb-4 border-b border-slate-800/80">
          <div className="flex items-center space-x-3">
            <div className="w-9 h-9 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
              <Server className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-white tracking-tight flex items-center gap-2">
                <span>Durable Background Task Queue</span>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-300 border border-purple-500/20 font-mono">
                  SQLite ACID WAL
                </span>
              </h2>
              <p className="text-[11px] text-slate-400">
                Persistent background queue with crash recovery and exponential backoff workers
              </p>
            </div>
          </div>

          {/* Actions & Filters */}
          <div className="flex flex-wrap items-center gap-2.5">
            {/* Status filters */}
            <div className="flex items-center bg-slate-900 p-1 rounded-xl border border-slate-800 text-xs">
              {['ALL', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED'].map((st) => (
                <button
                  key={st}
                  onClick={() => setFilterStatus(st)}
                  className={`px-2.5 py-1 rounded-lg font-semibold transition-all cursor-pointer ${
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
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl border text-xs font-semibold transition-all cursor-pointer ${
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
              className="p-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 transition-colors cursor-pointer"
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
                  <th className="py-3 px-4 text-right">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono text-slate-300">
                {jobs.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="text-center py-8 text-slate-500 font-sans">
                      No background webhook jobs found. Incoming GitHub webhooks or simulated events will durably appear here.
                    </td>
                  </tr>
                ) : (
                  jobs.map((job) => (
                    <tr key={job.job_id} className="hover:bg-slate-900/40 transition-colors">
                      <td className="py-3 px-4 font-bold text-indigo-400">{job.job_id.slice(0, 14)}...</td>
                      <td className="py-3 px-4 font-sans font-medium text-slate-200">
                        <div className="flex items-center gap-1.5">
                          <GitPullRequest className="w-3.5 h-3.5 text-indigo-400 shrink-0" />
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
                        {formatTimestamp(job.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => setSelectedJob(job)}
                          className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 font-sans text-[11px] transition-colors cursor-pointer"
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
                  className="text-slate-400 hover:text-white text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 cursor-pointer"
                >
                  ✕ Close
                </button>
              </div>

              <div className="space-y-3 text-xs">
                <div className="grid grid-cols-2 gap-2 text-slate-300">
                  <div><strong>PR:</strong> {selectedJob.pr_identifier}</div>
                  <div><strong>Status:</strong> {selectedJob.status}</div>
                  <div><strong>Created:</strong> {formatDateTime(selectedJob.created_at)}</div>
                  <div><strong>Last Updated:</strong> {formatDateTime(selectedJob.updated_at)}</div>
                </div>


                {selectedJob.error_message && (
                  <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/20 text-rose-300">
                    <strong>Error / Notice:</strong> {selectedJob.error_message}
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
    </div>
  );
};

