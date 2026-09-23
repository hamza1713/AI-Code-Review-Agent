import { useEffect, useState } from 'react';
import { Loader2, Clock, ShieldCheck, GitGraph, FileCheck2, Cpu } from 'lucide-react';

interface PipelineProgressProps {
  currentStage?: string | null;
  stageProgress?: number | null;
}

const STAGE_LABELS: Record<string, string> = {
  QUEUED: 'Queued — Waiting for an available review worker...',
  PROCESSING: 'Claimed by worker — Initializing review environment...',
  INGESTION: 'Ingesting diff, resolving AST & symbol references...',
  AST_PRESCAN: 'Running compiler-grade AST analysis & call graphs...',
  SECURITY_SCAN: 'Scanning for OWASP Top 10 vulnerabilities & security sinks...',
  GOVERNANCE_EVAL: 'Evaluating custom team governance rules & standards...',
  LLM_REASONING: 'Multi-agent reasoning crew formulating architectural review...',
  TEST_SANDBOX: 'Running isolated empirical sandbox test verification...',
  SYNTHESIS: 'Synthesizing final executive verdict & actionable inline fixes...',
  COMPLETED: 'Review finalized — rendering report...',
};

export function PipelineProgress({ currentStage, stageProgress }: PipelineProgressProps) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setSeconds(value => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  const progressPercent = typeof stageProgress === 'number'
    ? Math.round(stageProgress > 1 ? stageProgress : stageProgress * 100)
    : null;

  const stageDescription = (currentStage && STAGE_LABELS[currentStage]) || (
    currentStage ? `Executing stage: ${currentStage}` : 'Waiting for the review service. Your report will appear here when it is ready.'
  );

  return (
    <section className="progress-panel" aria-busy="true" aria-labelledby="progress-title">
      <div className="progress-symbol"><Loader2 size={28} className="animate-spin" /></div>
      <p className="eyebrow">REVIEW IN PROGRESS</p>
      <h1 id="progress-title">Taking a closer look at your code.</h1>
      
      {currentStage && (
        <div className="flex items-center justify-center gap-2 text-indigo-400 font-medium text-sm my-2">
          <Cpu size={16} />
          <span>Stage: <strong>{currentStage}</strong></span>
          {progressPercent !== null && <span className="text-slate-400">({progressPercent}%)</span>}
        </div>
      )}

      {progressPercent !== null && (
        <div className="w-full max-w-md mx-auto bg-slate-800 rounded-full h-2.5 overflow-hidden my-3 border border-slate-700">
          <div
            className="bg-indigo-500 h-2.5 rounded-full transition-all duration-500 ease-out"
            style={{ width: `${Math.max(5, Math.min(100, progressPercent))}%` }}
          />
        </div>
      )}

      <p role="status" className="text-sm text-slate-300 max-w-lg mx-auto">{stageDescription}</p>
      
      <div className="elapsed"><Clock size={15} />{seconds}s elapsed</div>
      {seconds >= 60 && (
        <p className="text-amber-300 text-xs">This review is taking a little longer. Larger changes or multi-agent crew reasoning can take up to three minutes.</p>
      )}
      
      <div className="progress-capabilities">
        <span><ShieldCheck size={17} />Security analysis</span>
        <span><GitGraph size={17} />Code impact</span>
        <span><FileCheck2 size={17} />Team standards</span>
      </div>
      <p className="progress-note">
        {currentStage ? '⚡ Live stage telemetry connected to distributed worker process.' : 'Live telemetry connects automatically as worker advances.'}
      </p>
    </section>
  );
}

