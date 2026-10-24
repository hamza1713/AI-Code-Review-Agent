import { useEffect, useState } from 'react';
import {
  Loader2,
  Clock,
  ShieldCheck,
  GitGraph,
  Cpu,
  TestTube2,
  FileSearch,
  CheckCircle2,
  Lock,
} from 'lucide-react';

interface PipelineProgressProps {
  currentStage?: string | null;
  stageProgress?: number | null;
}

const STAGE_STEPS = [
  { id: 'INGESTION', label: 'Diff Ingestion', icon: FileSearch, minProgress: 0.1 },
  { id: 'AST_PRESCAN', label: 'Polyglot AST', icon: GitGraph, minProgress: 0.3 },
  { id: 'SECURITY_SCAN', label: 'Threat & Supply Chain', icon: ShieldCheck, minProgress: 0.5 },
  { id: 'LLM_REASONING', label: 'Adversarial Crew', icon: Cpu, minProgress: 0.75 },
  { id: 'TEST_SANDBOX', label: 'Sandbox Proof', icon: TestTube2, minProgress: 0.88 },
  { id: 'SYNTHESIS', label: 'Verdict & Attestation', icon: CheckCircle2, minProgress: 0.95 },
];

const STAGE_LABELS: Record<string, string> = {
  QUEUED: 'Queued — Waiting for an available review worker...',
  PROCESSING: 'Claimed by worker — Initializing review environment...',
  INGESTION: 'Ingesting diff, validating paths, and building symbol references...',
  AST_PRESCAN: 'Running polyglot Tree-Sitter AST & inter-procedural taint graph...',
  SECURITY_SCAN: 'Scanning for supply-chain slopsquatting, Trojan Source & OWASP sinks...',
  AI_THREAT_SCAN: 'Detecting unawaited async calls, hollow stubs & contract drift...',
  GOVERNANCE_EVAL: 'Evaluating custom team governance rules & standards...',
  LLM_REASONING: 'Multi-agent crew formulating architectural review & adversarial consensus...',
  TEST_SANDBOX: 'Executing isolated container sandbox test verification & self-healing...',
  SYNTHESIS: 'Synthesizing final executive verdict, cryptographic receipt & inline fixes...',
  COMPLETED: 'Review finalized — rendering interactive workbench...',
};

export function PipelineProgress({ currentStage, stageProgress }: PipelineProgressProps) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setSeconds(val => val + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  const progressPercent = typeof stageProgress === 'number'
    ? Math.round(stageProgress > 1 ? stageProgress : stageProgress * 100)
    : Math.min(95, Math.max(10, seconds * 5));

  const stageDescription = (currentStage && STAGE_LABELS[currentStage]) || (
    currentStage
      ? `Executing stage: ${currentStage}`
      : 'Review pipeline active. Analyzing multi-file context, security boundaries, and patch safety...'
  );

  const getStepStatus = (stepId: string, minProgress: number) => {
    if (!currentStage) return (progressPercent / 100) >= minProgress ? 'completed' : 'pending';
    const currentIndex = STAGE_STEPS.findIndex(s => s.id === currentStage);
    const stepIndex = STAGE_STEPS.findIndex(s => s.id === stepId);

    if (currentStage === stepId || (currentStage === 'AI_THREAT_SCAN' && stepId === 'SECURITY_SCAN')) {
      return 'active';
    }
    if (currentIndex > stepIndex || (progressPercent / 100) >= minProgress) {
      return 'completed';
    }
    return 'pending';
  };

  return (
    <section className="bg-[#0e121a] border border-slate-800 rounded-2xl p-6 sm:p-8 max-w-4xl mx-auto shadow-2xl my-6" aria-busy="true" aria-labelledby="progress-title">
      <div className="flex flex-col items-center text-center">
        {/* Top Spinner Badge */}
        <div className="relative mb-4">
          <div className="w-16 h-16 rounded-2xl bg-indigo-500/10 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shadow-inner">
            <Loader2 size={32} className="animate-spin text-indigo-400" />
          </div>
          <div className="absolute -bottom-1 -right-1 bg-emerald-500 text-[10px] text-black font-bold px-1.5 py-0.5 rounded-full border border-black flex items-center gap-0.5">
            <Lock size={10} />
            <span>v3.0</span>
          </div>
        </div>

        <p className="text-[11px] font-mono tracking-widest text-indigo-400 uppercase font-semibold">
          Zero-Trust Multi-Agent Verification Pipeline
        </p>
        <h1 id="progress-title" className="text-xl sm:text-2xl font-bold text-white mt-1 mb-2">
          Verifying Code Quality, Logic & Security
        </h1>

        {/* Live Stage Pill & Percentage */}
        <div className="flex items-center gap-3 bg-slate-900/90 border border-slate-800 px-4 py-1.5 rounded-full my-3 shadow-sm">
          <Cpu size={15} className="text-indigo-400 animate-pulse" />
          <span className="text-xs text-slate-300">
            Active: <strong className="text-white font-mono">{currentStage || 'PROCESSING'}</strong>
          </span>
          <span className="text-xs font-mono font-bold text-indigo-400 bg-indigo-950/60 px-2 py-0.5 rounded border border-indigo-900/80">
            {progressPercent}%
          </span>
        </div>

        {/* Global Progress Bar */}
        <div
          role="progressbar"
          aria-valuenow={progressPercent}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Review stage progress"
          className="w-full max-w-xl mx-auto bg-slate-900/80 rounded-full h-2.5 overflow-hidden my-3 border border-slate-800 shadow-inner"
        >
          <div
            className="bg-gradient-to-r from-indigo-500 via-cyan-400 to-emerald-400 h-2.5 rounded-full transition-all duration-700 ease-out"
            style={{ width: `${Math.max(6, Math.min(100, progressPercent))}%` }}
          />
        </div>

        {/* Dynamic Stage Description */}
        <p role="status" className="text-xs sm:text-sm text-slate-300 max-w-xl mx-auto leading-relaxed my-2">
          {stageDescription}
        </p>

        {/* Elapsed Timer */}
        <div className="flex items-center justify-center gap-1.5 text-xs text-slate-400 font-mono mt-1">
          <Clock size={13} className="text-slate-500" />
          <span>{seconds}s elapsed</span>
          {seconds >= 45 && (
            <span className="text-amber-400 ml-2">
              (Adversarial crew analysis & container sandbox active)
            </span>
          )}
        </div>
      </div>

      {/* 6-Stage Stepper Grid */}
      <div className="mt-8 pt-6 border-t border-slate-800/80 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {STAGE_STEPS.map((step) => {
          const status = getStepStatus(step.id, step.minProgress);
          const IconComponent = step.icon;
          return (
            <div
              key={step.id}
              className={`p-3 rounded-xl border flex flex-col items-center text-center transition-all ${
                status === 'active'
                  ? 'bg-indigo-950/40 border-indigo-500/50 shadow-md shadow-indigo-950/50'
                  : status === 'completed'
                  ? 'bg-emerald-950/20 border-emerald-500/30 text-emerald-300'
                  : 'bg-slate-900/40 border-slate-800 text-slate-500 opacity-60'
              }`}
            >
              <div className={`w-8 h-8 rounded-lg flex items-center justify-center mb-2 ${
                status === 'active'
                  ? 'bg-indigo-500/20 text-indigo-400 animate-pulse'
                  : status === 'completed'
                  ? 'bg-emerald-500/20 text-emerald-400'
                  : 'bg-slate-800 text-slate-500'
              }`}>
                <IconComponent size={16} />
              </div>
              <span className={`text-[11px] font-semibold leading-tight ${
                status === 'active' ? 'text-white' : status === 'completed' ? 'text-slate-200' : 'text-slate-500'
              }`}>
                {step.label}
              </span>
              <span className="text-[9px] font-mono mt-1 uppercase">
                {status === 'active' ? '● In Progress' : status === 'completed' ? '✓ Passed' : 'Pending'}
              </span>
            </div>
          );
        })}
      </div>

      <p className="text-[11px] text-center text-slate-400 font-mono mt-6">
        🔒 Provable Trust: Real-time telemetry streamed directly from AST parser, supply-chain verifier, and sandbox.
      </p>
    </section>
  );
}
