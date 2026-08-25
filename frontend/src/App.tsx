import { useState, useEffect } from 'react';
import { Navbar } from './components/Navbar';
import { ReviewInput } from './components/ReviewInput';
import { PipelineProgress } from './components/PipelineProgress';
import { ReviewDashboard } from './components/ReviewDashboard';
import { JobsQueueMonitor } from './components/JobsQueueMonitor';
import type { ReviewAPIResponse, HealthStatus } from './types/review';
import { AlertTriangle } from 'lucide-react';

export function App() {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [activeView, setActiveView] = useState<'review' | 'jobs'>('review');
  const [isLoading, setIsLoading] = useState(false);
  const [reviewResult, setReviewResult] = useState<ReviewAPIResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Poll backend health status
  const checkHealth = async () => {
    try {
      const res = await fetch('/health');
      if (res.ok) {
        const data = await res.json();
        setHealth(data);
      } else {
        setHealth(null);
      }
    } catch {
      setHealth(null);
    }
  };

  useEffect(() => {
    checkHealth();
    const interval = setInterval(checkHealth, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleReviewSubmit = async (payload: {
    rawDiff?: string;
    file?: File;
    zipFile?: File;
  }) => {
    setIsLoading(true);
    setErrorMessage(null);
    setReviewResult(null);

    try {
      let response: Response;

      if (payload.rawDiff) {
        response = await fetch('/api/review', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ raw_diff: payload.rawDiff }),
        });
      } else if (payload.file) {
        const formData = new FormData();
        formData.append('file', payload.file);
        response = await fetch('/api/review', {
          method: 'POST',
          body: formData,
        });
      } else if (payload.zipFile) {
        const formData = new FormData();
        formData.append('zip_file', payload.zipFile);
        response = await fetch('/api/review', {
          method: 'POST',
          body: formData,
        });
      } else {
        throw new Error('No input provided for review.');
      }

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        const detail = errorData.detail || `HTTP ${response.status}: ${response.statusText}`;
        throw new Error(detail);
      }

      const resultData: ReviewAPIResponse = await response.json();
      setReviewResult(resultData);
    } catch (err: any) {
      console.error('Code review error:', err);
      setErrorMessage(err.message || 'An unexpected error occurred during review.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = () => {
    setReviewResult(null);
    setErrorMessage(null);
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
      <main className="flex-1 max-w-7xl w-full mx-auto px-3 sm:px-6 lg:px-8 py-5">
        {/* Global Error Banner */}
        {errorMessage && (
          <div className="mb-4 p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs flex items-center justify-between shadow-md">
            <div className="flex items-center gap-2.5">
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
              <div>
                <strong className="font-semibold mr-1">Review Error:</strong>
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
        {activeView === 'review' && (
          <div className="space-y-4">
            {/* Input form when no active result and not loading */}
            {!isLoading && !reviewResult && (
              <ReviewInput
                onSubmit={handleReviewSubmit}
                isLoading={isLoading}
              />
            )}

            {/* Pipeline Progress while loading */}
            {isLoading && <PipelineProgress />}

            {/* Results Dashboard when completed */}
            {!isLoading && reviewResult && (
              <ReviewDashboard
                data={reviewResult}
                onReset={handleReset}
              />
            )}
          </div>
        )}

        {/* View 2: Durable Webhook Task Queue Monitor */}
        {activeView === 'jobs' && <JobsQueueMonitor />}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-[#0d1117]/80 py-3.5 text-center text-xs text-slate-500">
        <div className="max-w-7xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between gap-2">
          <span>AI Code Review Agent (Enterprise Edition v2.0) • Powered by CrewAI Flows & Google Gemini</span>
          <span className="font-mono text-[11px] text-slate-600">OASIS SARIF v2.1 • AST Call Graph • SQLite Queue</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
