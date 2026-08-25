import React, { useState, useEffect } from 'react';
import { Search, ShieldAlert, GitGraph, FileCheck2, Cpu, Loader2, CheckCircle2, Clock } from 'lucide-react';

interface Stage {
  id: number;
  title: string;
  subtitle: string;
  icon: React.ReactNode;
}

const STAGES: Stage[] = [
  {
    id: 1,
    title: 'Unified Diff Parsing & Chunking',
    subtitle: 'Extracts file hunks and builds 1-indexed target line maps',
    icon: <Search className="w-4 h-4 text-cyan-400" />
  },
  {
    id: 2,
    title: 'Quick Pattern Scanner (Heuristic AppSec)',
    subtitle: 'Heuristic regex scan for SQL injection, secrets, and auth anti-patterns',
    icon: <ShieldAlert className="w-4 h-4 text-rose-400" />
  },
  {
    id: 3,
    title: 'AST Code Graph & Caller Resolution',
    subtitle: 'Indexes qualified symbols and computes cross-file impact dependencies',
    icon: <GitGraph className="w-4 h-4 text-indigo-400" />
  },
  {
    id: 4,
    title: 'Team Governance Rules Evaluation',
    subtitle: 'Validates changes against .code-review.yaml coding standards',
    icon: <FileCheck2 className="w-4 h-4 text-amber-400" />
  },
  {
    id: 5,
    title: 'CrewAI Multi-Agent Synthesis',
    subtitle: 'Senior Developer + Security Engineer + Tech Lead parallel deliberation',
    icon: <Cpu className="w-4 h-4 text-purple-400" />
  }
];

export const PipelineProgress: React.FC = () => {
  const [activeStage, setActiveStage] = useState(1);
  const [secondsElapsed, setSecondsElapsed] = useState(0);

  // Advance stage animation
  useEffect(() => {
    const stageTimer = setInterval(() => {
      setActiveStage(prev => (prev < STAGES.length ? prev + 1 : prev));
    }, 1800);

    const clockTimer = setInterval(() => {
      setSecondsElapsed(prev => prev + 1);
    }, 1000);

    return () => {
      clearInterval(stageTimer);
      clearInterval(clockTimer);
    };
  }, []);

  return (
    <div className="bg-[#12151c] border border-indigo-500/20 rounded-2xl p-8 shadow-2xl relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-indigo-500/5 via-purple-500/5 to-transparent pointer-events-none" />

      {/* Header */}
      <div className="flex items-center justify-between pb-6 border-b border-slate-800/80 relative z-10">
        <div className="flex items-center space-x-4">
          <div className="w-10 h-10 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
          <div>
            <h3 className="text-base font-bold text-white tracking-tight">
              Executing Multi-Agent Review Pipeline...
            </h3>
            <p className="text-xs text-slate-400">
              Running deterministic AST scans, governance rules, and LLM synthesis
            </p>
          </div>
        </div>

        {/* Stopwatch */}
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 text-xs font-mono text-indigo-300">
          <Clock className="w-3.5 h-3.5" />
          <span>Elapsed: {secondsElapsed}s</span>
        </div>
      </div>

      {/* Pipeline Stages */}
      <div className="mt-8 space-y-4 relative z-10">
        {STAGES.map((stage) => {
          const isCompleted = activeStage > stage.id;
          const isCurrent = activeStage === stage.id;

          return (
            <div
              key={stage.id}
              className={`flex items-start gap-4 p-4 rounded-xl border transition-all duration-500 ${
                isCurrent
                  ? 'bg-indigo-950/30 border-indigo-500/40 shadow-lg shadow-indigo-500/5 ring-1 ring-indigo-500/20'
                  : isCompleted
                  ? 'bg-slate-900/40 border-slate-800/60 opacity-80'
                  : 'bg-slate-900/20 border-slate-800/30 opacity-40'
              }`}
            >
              {/* Step indicator */}
              <div
                className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 border text-xs font-mono font-bold transition-all ${
                  isCompleted
                    ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-400'
                    : isCurrent
                    ? 'bg-indigo-600 border-indigo-400 text-white animate-pulse'
                    : 'bg-slate-800 border-slate-700 text-slate-500'
                }`}
              >
                {isCompleted ? <CheckCircle2 className="w-4 h-4" /> : stage.id}
              </div>

              {/* Stage description */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-xs text-slate-200">{stage.title}</span>
                  {isCurrent && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 font-mono animate-pulse">
                      In Progress
                    </span>
                  )}
                  {isCompleted && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">
                      Done
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-slate-400 mt-0.5">{stage.subtitle}</p>
              </div>

              {/* Icon */}
              <div className="shrink-0 p-2 rounded-lg bg-slate-800/50 border border-slate-700/40">
                {stage.icon}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
