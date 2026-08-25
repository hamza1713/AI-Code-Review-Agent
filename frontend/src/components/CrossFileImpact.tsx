import React from 'react';
import { GitGraph, CornerDownRight, CheckCircle2 } from 'lucide-react';
import type { CrossFileImpactSummary } from '../types/review';

interface CrossFileImpactProps {
  impact: CrossFileImpactSummary;
}

export const CrossFileImpact: React.FC<CrossFileImpactProps> = ({ impact }) => {
  const callersMap = impact.impacted_callers || {};
  const hasCallers = Object.keys(callersMap).length > 0;

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between pb-3 border-b border-slate-800/80">
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-lg bg-cyan-500/10 border border-cyan-500/20 flex items-center justify-center text-cyan-400">
            <GitGraph className="w-3.5 h-3.5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-bold text-white tracking-tight uppercase">Cross-File Impact Analysis</h3>
              <span className={`text-[9px] uppercase font-mono font-bold px-1.5 py-0.5 rounded-full border ${
                impact.is_python
                  ? 'bg-cyan-500/10 text-cyan-300 border-cyan-500/20'
                  : 'bg-slate-800 text-slate-400 border-slate-700'
              }`}>
                {impact.is_python ? 'Python AST' : 'Non-Python'}
              </span>
            </div>
            <p className="text-[10px] text-slate-400">AST symbol caller resolution across repository</p>
          </div>
        </div>
      </div>

      {/* Message and Callers */}
      <div className="mt-3 space-y-2 max-h-[380px] overflow-y-auto pr-1 text-xs">
        {impact.message && (
          <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800 text-[11px] text-slate-300 font-mono leading-relaxed">
            {impact.message}
          </div>
        )}

        {hasCallers ? (
          <div className="space-y-2 mt-2">
            <span className="text-[11px] font-semibold text-slate-300 block">
              Modified Functions & Impacted Callers:
            </span>
            {Object.entries(callersMap).map(([func, callers], idx) => (
              <div
                key={idx}
                className="bg-slate-900/70 border border-slate-800/80 rounded-lg p-2.5 space-y-1.5"
              >
                <div className="flex items-center gap-1.5 font-mono text-[11px] text-cyan-300 font-bold">
                  <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" />
                  <span>{func}</span>
                </div>

                <div className="pl-3 space-y-1 border-l border-slate-800">
                  {callers.length === 0 ? (
                    <span className="text-[10px] text-slate-500 italic">No external callers identified.</span>
                  ) : (
                    callers.map((caller, cIdx) => (
                      <div
                        key={cIdx}
                        className="flex items-center gap-1.5 text-[10px] font-mono text-slate-300 bg-[#0a0c10] px-2 py-0.5 rounded border border-slate-800/60"
                      >
                        <CornerDownRight className="w-2.5 h-2.5 text-slate-500 shrink-0" />
                        <span>{caller}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          !impact.message && (
            <div className="py-4 px-3 text-center border border-dashed border-slate-800 rounded-lg bg-slate-900/30">
              <div className="flex items-center justify-center gap-2 text-xs text-cyan-400 font-medium">
                <CheckCircle2 className="w-4 h-4" />
                <span>No cross-file caller dependencies affected.</span>
              </div>
            </div>
          )
        )}
      </div>
    </div>
  );
};
