import React, { useState } from 'react';
import { ShieldAlert, Copy, Check, Filter, CheckCircle2 } from 'lucide-react';
import type { SastFinding, Severity } from '../types/review';

interface FindingsListProps {
  findings: SastFinding[];
  label: string;
}

export const FindingsList: React.FC<FindingsListProps> = ({ findings, label }) => {
  const [selectedSeverity, setSelectedSeverity] = useState<string>('ALL');
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  const filtered = selectedSeverity === 'ALL'
    ? findings
    : findings.filter(f => f.severity.toUpperCase() === selectedSeverity);

  const handleCopyFix = (text: string, idx: number) => {
    navigator.clipboard.writeText(text);
    setCopiedIndex(idx);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const getSeverityBadge = (sev: Severity) => {
    const s = sev.toUpperCase();
    if (s === 'CRITICAL') {
      return 'bg-rose-500/20 text-rose-300 border-rose-500/40';
    }
    if (s === 'HIGH') {
      return 'bg-orange-500/20 text-orange-300 border-orange-500/40';
    }
    if (s === 'MEDIUM') {
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    }
    return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between pb-3 border-b border-slate-800/80 gap-2">
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-lg bg-rose-500/10 border border-rose-500/20 flex items-center justify-center text-rose-400">
            <ShieldAlert className="w-3.5 h-3.5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-bold text-white tracking-tight uppercase">Security Pattern Findings</h3>
              <span className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono font-bold ${
                findings.length > 0
                  ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                  : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
              }`}>
                {findings.length}
              </span>
            </div>
            <p className="text-[10px] text-slate-400">{label}</p>
          </div>
        </div>

        {/* Filter */}
        {findings.length > 0 && (
          <div className="flex items-center gap-1 text-xs bg-slate-900/80 p-0.5 rounded-lg border border-slate-800">
            <Filter className="w-2.5 h-2.5 text-slate-500 ml-1" />
            {['ALL', 'CRITICAL', 'HIGH', 'MEDIUM'].map((sev) => (
              <button
                key={sev}
                onClick={() => setSelectedSeverity(sev)}
                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold transition-all ${
                  selectedSeverity === sev
                    ? 'bg-slate-700 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {sev}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Findings List */}
      <div className="mt-3 space-y-2.5 max-h-[380px] overflow-y-auto pr-1">
        {filtered.length === 0 ? (
          <div className="py-4 px-3 text-center border border-dashed border-slate-800 rounded-lg bg-slate-900/30">
            <div className="flex items-center justify-center gap-2 text-xs text-emerald-400 font-medium">
              <CheckCircle2 className="w-4 h-4" />
              <span>{findings.length === 0 ? 'No security anti-patterns detected.' : 'No findings for this filter.'}</span>
            </div>
          </div>
        ) : (
          filtered.map((item, idx) => (
            <div
              key={idx}
              className="bg-slate-900/70 border border-slate-800/80 rounded-lg p-3 space-y-2 hover:border-slate-700 transition-all text-xs"
            >
              {/* Finding Top Bar */}
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <div className="flex items-center gap-1.5">
                  <span className={`text-[9px] uppercase font-mono font-bold px-1.5 py-0.5 rounded border ${getSeverityBadge(item.severity)}`}>
                    {item.severity}
                  </span>
                  <span className="font-mono font-semibold text-slate-200 text-[11px]">
                    {item.rule_id}
                  </span>
                  {item.cwe && (
                    <span className="text-[9px] font-mono px-1 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700/50">
                      {item.cwe}
                    </span>
                  )}
                </div>

                <div className="text-[10px] font-mono text-indigo-300 bg-indigo-950/40 px-1.5 py-0.5 rounded border border-indigo-900/40">
                  {item.file_path}:L{item.line_number}
                </div>
              </div>

              {/* Description */}
              <p className="text-slate-300 text-[11px] leading-relaxed">
                {item.description}
              </p>

              {/* Snippet */}
              {item.snippet && (
                <div className="rounded bg-[#0a0c10] border border-slate-800/80 p-2 overflow-x-auto">
                  <pre className="text-[10px] font-mono text-rose-300 leading-tight">
                    <code>{item.snippet}</code>
                  </pre>
                </div>
              )}

              {/* Suggested Fix */}
              {item.fix_recommendation && (
                <div className="bg-emerald-950/20 border border-emerald-500/20 rounded p-2 text-[11px]">
                  <div className="flex items-center justify-between gap-2 text-emerald-400 font-semibold mb-0.5">
                    <span className="text-[10px] flex items-center gap-1">
                      💡 Suggested Fix:
                    </span>
                    <button
                      onClick={() => handleCopyFix(item.fix_recommendation, idx)}
                      className="text-[9px] text-emerald-300 hover:text-white flex items-center gap-1 px-1.5 py-0.5 rounded bg-emerald-900/40 hover:bg-emerald-900/70 transition-colors cursor-pointer"
                    >
                      {copiedIndex === idx ? (
                        <>
                          <Check className="w-2.5 h-2.5 text-emerald-400" />
                          <span>Copied</span>
                        </>
                      ) : (
                        <>
                          <Copy className="w-2.5 h-2.5" />
                          <span>Copy</span>
                        </>
                      )}
                    </button>
                  </div>
                  <p className="text-slate-300 text-[10px] leading-relaxed">
                    {item.fix_recommendation}
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
