import { useState, useEffect, Suspense, lazy, type ReactNode } from 'react';
import { apiFetch, setOperatorToken } from './api';
import { OperatorConnection } from './components/OperatorConnection';
import { Navbar } from './components/Navbar';
import { ReviewInput } from './components/ReviewInput';
import { PipelineProgress } from './components/PipelineProgress';
import { ReviewDashboard } from './components/ReviewDashboard';
import type { HealthStatus } from './types/review';
import { AlertTriangle, ShieldCheck, GitGraph, FileCheck2, ArrowUpRight, Loader2 } from 'lucide-react';
import { useHashView } from './hooks/useHashView';
import { useTheme } from './hooks/useTheme';
import { useReviewRunner } from './hooks/useReviewRunner';

// Secondary views are code-split.
const JobsQueueMonitor = lazy(() =>
  import('./components/JobsQueueMonitor').then(m => ({ default: m.JobsQueueMonitor }))
);
const CalibrationView = lazy(() =>
  import('./components/CalibrationView').then(m => ({ default: m.CalibrationView }))
);
const LabelingWorkbench = lazy(() =>
  import('./components/LabelingWorkbench').then(m => ({ default: m.LabelingWorkbench }))
);

function ViewLoader({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center gap-2 p-12 text-ink-muted">
          <Loader2 className="h-5 w-5 animate-spin text-indigo-400" />
          <span className="text-sm">Loading {label}…</span>
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export function App() {
  const [connected, setConnected] = useState(false);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [activeView, setActiveView] = useHashView();
  const theme = useTheme();
  const review = useReviewRunner();

  useEffect(() => {
    const controller = new AbortController();
    const checkHealth = async () => {
      try {
        const response = await fetch('/health', { signal: controller.signal });
        const data = response.ok ? await response.json() : null;
        if (!controller.signal.aborted) setHealth(data);
      } catch { if (!controller.signal.aborted) setHealth(null); }
    };
    const checkSession = async () => {
      try {
        const response = await apiFetch('/api/session', { signal: controller.signal });
        if (response.ok && !controller.signal.aborted) setConnected(true);
      } catch {
        // A token is required; stay on the login screen.
      }
    };
    void checkHealth();
    void checkSession();
    const interval = setInterval(checkHealth, 10000);
    return () => { controller.abort(); clearInterval(interval); };
  }, []);

  const { cancel } = review;
  useEffect(() => {
    const expired = () => { cancel(); setOperatorToken(''); setConnected(false); };
    window.addEventListener('operator-expired', expired);
    return () => window.removeEventListener('operator-expired', expired);
  }, [cancel]);

  const handleReset = () => {
    review.reset();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  if (!connected) {
    return <OperatorConnection onConnected={() => { setConnected(true); void review.resume(); }} />;
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas font-sans text-ink selection:bg-indigo-500/30">
      <Navbar
        health={health}
        activeView={activeView}
        onViewChange={setActiveView}
        theme={theme.preference}
        onCycleTheme={theme.cycle}
        onLogout={() => {
          review.cancel(); review.reset(); setOperatorToken(''); setConnected(false);
        }}
      />

      <main id="main-content" tabIndex={-1} className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 focus:outline-none sm:px-6 lg:px-8">
        {review.error && (
          <div role="alert" className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3.5 text-sm text-rose-200">
            <div className="flex items-center gap-2.5">
              <AlertTriangle className="h-4 w-4 shrink-0 text-rose-400" />
              <div><span className="font-semibold">Review error: </span><span>{review.error}</span></div>
            </div>
            <button
              type="button"
              onClick={() => review.setError(null)}
              className="rounded bg-rose-950/40 px-2 py-0.5 text-xs text-rose-300 hover:text-rose-100"
            >
              Dismiss
            </button>
          </div>
        )}

        <div hidden={activeView !== 'review'} className="space-y-4">
          <div hidden={review.isLoading || !!review.result}>
            <section className="workspace-intro">
              <div>
                <p className="eyebrow">YOUR NEXT MERGE, WITH MORE CONFIDENCE</p>
                <h1>A second set of eyes.<br /><span>For every line that matters.</span></h1>
                <p>Find security risks, understand code changes, and turn feedback into a clear path forward.</p>
              </div>
              <span className="intro-label"><ShieldCheck size={16} />Evidence-first code review</span>
            </section>
            <div className="review-layout">
              <ReviewInput onSubmit={review.submit} isLoading={review.isLoading} />
              <aside className="review-guide" aria-label="Review guide">
                <p className="eyebrow">FROM CODE TO CLARITY</p>
                <h2>One review.<br />Three perspectives.</h2>
                <div className="guide-item"><ShieldCheck /><div><h3>Catch security risks</h3><p>Find suspicious patterns and get practical fix recommendations.</p></div></div>
                <div className="guide-item"><GitGraph /><div><h3>See the wider impact</h3><p>Understand how Python changes affect connected code.</p></div></div>
                <div className="guide-item"><FileCheck2 /><div><h3>Keep standards consistent</h3><p>Check changes against your configured team rules.</p></div></div>
                <div className="guide-note"><ArrowUpRight size={18} /><p><strong>Start small.</strong> Try a sample diff to explore the workflow. Running a review sends it to your configured analysis service.</p></div>
              </aside>
            </div>
          </div>

          {review.isLoading && (
            <>
              <PipelineProgress currentStage={review.stage} stageProgress={review.progress} />
              {review.activeJob && (
                <p className="mt-2 text-center text-xs text-ink-muted">
                  Job ID: <code className="text-ink">{review.activeJob}</code>. You can follow this review in the job queue.
                </p>
              )}
            </>
          )}

          {!review.isLoading && review.result && <ReviewDashboard data={review.result} onReset={handleReset} />}
        </div>

        {activeView === 'jobs' && (
          <ViewLoader label="job queue">
            <JobsQueueMonitor onSelectReview={result => { review.setResult(result); setActiveView('review'); }} />
          </ViewLoader>
        )}
        {activeView === 'calibration' && (
          <ViewLoader label="calibration"><CalibrationView /></ViewLoader>
        )}
        {activeView === 'labeling' && (
          <ViewLoader label="labeling workbench"><LabelingWorkbench /></ViewLoader>
        )}
      </main>

      <footer className="border-t border-line py-3.5 text-xs text-ink-muted">
        <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-2 px-4 sm:flex-row">
          <span>Code Review Agent · advisory reviews, human merge authority</span>
          <span className="font-mono">Review carefully. Ship confidently.</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
