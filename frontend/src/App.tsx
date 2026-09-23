import { useState, useEffect, useRef } from 'react';
import { apiFetch, responseError } from './api';
import { Navbar } from './components/Navbar';
import { ReviewInput } from './components/ReviewInput';
import { PipelineProgress } from './components/PipelineProgress';
import { ReviewDashboard } from './components/ReviewDashboard';
import { JobsQueueMonitor } from './components/JobsQueueMonitor';
import type { ReviewAPIResponse, HealthStatus } from './types/review';
import { AlertTriangle, ShieldCheck, GitGraph, FileCheck2, ArrowUpRight } from 'lucide-react';

export function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [activeView, setActiveView] = useState<'review' | 'jobs'>('review');
  const [isLoading, setIsLoading] = useState(false);
  const [reviewResult, setReviewResult] = useState<ReviewAPIResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const requestController = useRef<AbortController | null>(null);
  const [activeJob, setActiveJob] = useState<string | null>(null);
  const [activeJobStage, setActiveJobStage] = useState<string | null>(null);
  const [activeJobProgress, setActiveJobProgress] = useState<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const checkHealth = async () => {
      try {
        const response = await fetch('/health', { signal: controller.signal });
        const data = response.ok ? await response.json() : null;
        if (!controller.signal.aborted) setHealth(data);
      } catch { if (!controller.signal.aborted) setHealth(null); }
    };
    void checkHealth();
    const interval = setInterval(checkHealth, 10000);
    return () => { controller.abort(); clearInterval(interval); requestController.current?.abort(); };
  }, []);

  const waitForJob = async (jobId: string, signal: AbortSignal) => {
    setActiveJob(jobId);
    // Store only the opaque job ID so a refreshed session can reconnect to it.
    sessionStorage.setItem('review-job-id', jobId);
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      const response = await apiFetch(`/jobs/${encodeURIComponent(jobId)}`, { signal });
      if (!response.ok) throw new Error(await responseError(response));
      const job = await response.json();

      if (job.current_stage) setActiveJobStage(job.current_stage);
      if (typeof job.stage_progress === 'number') setActiveJobProgress(job.stage_progress);

      if (job.status === 'COMPLETED') {
        sessionStorage.removeItem('review-job-id');
        setActiveJobStage(null);
        setActiveJobProgress(null);
        return job.result as ReviewAPIResponse;
      }
      if (job.status === 'FAILED') {
        sessionStorage.removeItem('review-job-id');
        setActiveJobStage(null);
        setActiveJobProgress(null);
        throw new Error(job.last_error || 'The review failed. Please try again.');
      }
      if (job.status === 'CANCELLED') {
        sessionStorage.removeItem('review-job-id');
        setActiveJobStage(null);
        setActiveJobProgress(null);
        throw new Error(job.last_error || 'The review was cancelled.');
      }
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
        const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 1500);
        if (signal.aborted) abort(); else signal.addEventListener('abort', abort, { once: true });
      });
    }
    throw new Error('The review is still queued or running. Check the job queue for its saved result.');
  };

  const resumeReview = async () => {
    const jobId = sessionStorage.getItem('review-job-id');
    if (!jobId) return;
    const controller = new AbortController();
    requestController.current?.abort(); requestController.current = controller;
    setIsLoading(true); setErrorMessage(null);
    try { setReviewResult(await waitForJob(jobId, controller.signal)); }
    catch (reason) { if (!controller.signal.aborted) setErrorMessage(reason instanceof Error ? reason.message : 'Unable to resume review.'); }
    finally { if (!controller.signal.aborted) setIsLoading(false); }
  };

  useEffect(() => { void resumeReview(); }, []);

  const handleReviewSubmit = async (payload: {
    rawDiff?: string;
    file?: File;
    zipFile?: File;
    prUrl?: string;
  }) => {
    setIsLoading(true);
    setErrorMessage(null);
    setReviewResult(null);
    const controller = new AbortController();
    requestController.current?.abort(); requestController.current = controller;

    try {
      let response: Response;

      if (payload.prUrl) {
        response = await apiFetch('/api/review', {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            'Prefer': 'respond-async',
          },
          body: JSON.stringify({ pr_url: payload.prUrl }),
        });
      } else if (payload.rawDiff) {
        response = await apiFetch('/api/review', {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            'Prefer': 'respond-async',
          },
          body: JSON.stringify({ raw_diff: payload.rawDiff }),
        });
      } else if (payload.file) {
        const formData = new FormData();
        formData.append('file', payload.file);
        response = await apiFetch('/api/review', {
          method: 'POST',
          signal: controller.signal,
          headers: { Prefer: 'respond-async' },
          body: formData,
        });
      } else if (payload.zipFile) {
        const formData = new FormData();
        formData.append('zip_file', payload.zipFile);
        response = await apiFetch('/api/review', {
          method: 'POST',
          signal: controller.signal,
          headers: { Prefer: 'respond-async' },
          body: formData,
        });
      } else {
        throw new Error('No input provided for review.');
      }

      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json();
      const data: ReviewAPIResponse = response.status === 202
        ? await waitForJob(result.job_id, controller.signal) : result;
      if (!controller.signal.aborted) setReviewResult(data);
    } catch (err: unknown) {
      console.error('Code review error:', err);
      if (!controller.signal.aborted) setErrorMessage(err instanceof Error ? err.message : 'An unexpected error occurred during review.');
    } finally {
      if (!controller.signal.aborted) setIsLoading(false);
    }
  };

  const handleReset = () => {
    setReviewResult(null);
    setErrorMessage(null);
    setActiveJobStage(null);
    setActiveJobProgress(null);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <div className="min-h-screen bg-[#0a0c10] flex flex-col text-slate-100 font-sans selection:bg-indigo-500/30 selection:text-indigo-200">
      {/* Top Navigation */}
      <Navbar
        health={health}
        activeView={activeView}
        onViewChange={setActiveView}
      />

      {/* Main Container */}
      <main id="main-content" className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Global Error Banner */}
        {errorMessage && (
          <div role="alert" className="mb-4 p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs flex items-center justify-between shadow-md">
            <div className="flex items-center gap-2.5">
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
              <div>
                <span className="font-semibold">Review Error: </span>
                <span>{errorMessage}</span>
              </div>
            </div>
            <button
              onClick={() => setErrorMessage(null)}
              className="text-xs text-rose-400 hover:text-rose-200 px-2 py-0.5 rounded bg-rose-950/40 cursor-pointer"
            >
              Dismiss
            </button>
          </div>
        )}

        {/* View 1: Review Workspace */}
        <div hidden={activeView !== 'review'} className="space-y-4">
            {/* Input form when no active result and not loading */}
            <div hidden={isLoading || !!reviewResult}>
              <>
                <section className="workspace-intro">
                  <div><p className="eyebrow">YOUR NEXT MERGE, WITH MORE CONFIDENCE</p><h1>A second set of eyes.<br /><span>For every line that matters.</span></h1><p>Find security risks, understand code changes, and turn feedback into a clear path forward.</p></div>
                  <span className="intro-label"><ShieldCheck size={16} />AI-assisted code review</span>
                </section>
                <div className="review-layout">
                  <ReviewInput onSubmit={handleReviewSubmit} isLoading={isLoading} />
                  <aside className="review-guide" aria-label="Review guide">
                    <p className="eyebrow">FROM CODE TO CLARITY</p><h2>One review.<br />Three perspectives.</h2>
                    <div className="guide-item"><ShieldCheck /><div><h3>Catch security risks</h3><p>Find suspicious patterns and get practical fix recommendations.</p></div></div>
                    <div className="guide-item"><GitGraph /><div><h3>See the wider impact</h3><p>Understand how Python changes affect connected code.</p></div></div>
                    <div className="guide-item"><FileCheck2 /><div><h3>Keep standards consistent</h3><p>Check changes against your configured team rules.</p></div></div>
                    <div className="guide-note"><ArrowUpRight size={18} /><p><strong>Start small.</strong> Try a sample diff to explore the workflow. Running a review sends it to your configured analysis service.</p></div>
                  </aside>
                </div>
              </>
            </div>

            {/* Pipeline Progress while loading */}
            {isLoading && (
              <>
                <PipelineProgress currentStage={activeJobStage} stageProgress={activeJobProgress} />
                {activeJob && <p className="text-xs text-slate-400 text-center mt-2">Job ID: <code className="text-slate-300">{activeJob}</code>. You can follow this review in the job queue.</p>}
              </>
            )}

            {/* Results Dashboard when completed */}
            {!isLoading && reviewResult && (
              <ReviewDashboard
                data={reviewResult}
                onReset={handleReset}
              />
            )}
          </div>

        {/* View 2: Durable Webhook Task Queue Monitor */}
        {activeView === 'jobs' && <JobsQueueMonitor />}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-[#0d1117]/80 py-3.5 text-center text-xs text-slate-500">
        <div className="max-w-7xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between gap-2">
          <span>Code Review Agent · Built for thoughtful code reviews</span>
          <span className="font-mono text-[11px] text-slate-600">Review carefully. Ship confidently.</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
