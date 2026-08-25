import React from 'react';
import { FileCheck, CheckCircle2 } from 'lucide-react';
import type { RuleViolation, RuleSeverity } from '../types/review';

interface GovernanceViolationsProps {
  violations: RuleViolation[];
}

export const GovernanceViolations: React.FC<GovernanceViolationsProps> = ({ violations }) => {
  const getSeverityBadge = (sev: RuleSeverity) => {
    const s = sev.toUpperCase();
    if (s === 'BLOCKING') {
      return 'bg-rose-500/20 text-rose-300 border-rose-500/40';
    }
    if (s === 'WARNING') {
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    }
    return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between pb-3 border-b border-slate-800/80">
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-lg bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-400">
            <FileCheck className="w-3.5 h-3.5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-bold text-white tracking-tight uppercase">Team Governance Rules</h3>
              <span className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono font-bold ${
                violations.length > 0
                  ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                  : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
              }`}>
                {violations.length}
              </span>
            </div>
            <p className="text-[10px] text-slate-400">Policy constraints defined in .code-review.yaml</p>
          </div>
        </div>
      </div>

      {/* List */}
      <div className="mt-3 space-y-2.5 max-h-[380px] overflow-y-auto pr-1">
        {violations.length === 0 ? (
          <div className="py-4 px-3 text-center border border-dashed border-slate-800 rounded-lg bg-slate-900/30">
            <div className="flex items-center justify-center gap-2 text-xs text-emerald-400 font-medium">
              <CheckCircle2 className="w-4 h-4" />
              <span>All team governance rules passed (.code-review.yaml).</span>
            </div>
          </div>
        ) : (
          violations.map((item, idx) => (
            <div
              key={idx}
              className="bg-slate-900/70 border border-slate-800/80 rounded-lg p-3 space-y-1.5 hover:border-slate-700 transition-all text-xs"
            >
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <div className="flex items-center gap-1.5">
                  <span className={`text-[9px] uppercase font-mono font-bold px-1.5 py-0.5 rounded border ${getSeverityBadge(item.severity)}`}>
                    {item.severity}
                  </span>
                  <span className="font-mono font-bold text-slate-200 text-[11px]">
                    {item.rule_name}
                  </span>
                  <span className="text-[9px] font-mono text-slate-400 bg-slate-800 px-1 py-0.5 rounded">
                    {item.rule_id}
                  </span>
                </div>

                <div className="text-[10px] font-mono text-indigo-300 bg-indigo-950/40 px-1.5 py-0.5 rounded border border-indigo-900/40">
                  {item.file_path}:L{item.line_number}
                </div>
              </div>

              <p className="text-slate-300 text-[11px] leading-relaxed">
                {item.description}
              </p>

              {item.suggested_fix && (
                <div className="bg-amber-950/20 border border-amber-500/20 rounded p-2 text-[10px] text-amber-200">
                  <span className="font-semibold block mb-0.5 text-amber-300">
                    🛠️ Action Required:
                  </span>
                  <p className="text-slate-300">
                    {item.suggested_fix}
                  </p>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};
