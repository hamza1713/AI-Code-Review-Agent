import React from 'react';
import { ShieldCheck, AlertTriangle, ShieldAlert, Sparkles, RefreshCw, Download } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface VerdictBannerProps {
  verdict: string;
  confidenceScore: number;
  summary: string;
  onReset: () => void;
  onExportJSON: () => void;
}

export const VerdictBanner: React.FC<VerdictBannerProps> = ({
  verdict,
  confidenceScore,
  summary,
  onReset,
  onExportJSON
}) => {
  const normVerdict = (verdict || 'APPROVE').toUpperCase();

  const isEscalate = normVerdict.includes('ESCALATE');
  const isRequestChanges = normVerdict.includes('REQUEST') || normVerdict.includes('CHANGE');

  let bannerStyle = 'border-emerald-500/30 bg-gradient-to-r from-emerald-950/30 via-slate-900/60 to-slate-900/40';
  let badgeStyle = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
  let gaugeColor = 'text-emerald-400';
  let gaugeBar = 'bg-emerald-500';
  let verdictTitle = 'APPROVE — Merge Safe';
  let VerdictIcon = ShieldCheck;

  if (isEscalate) {
    bannerStyle = 'border-rose-500/40 bg-gradient-to-r from-rose-950/40 via-slate-900/70 to-slate-900/50';
    badgeStyle = 'bg-rose-500/20 text-rose-300 border-rose-500/40';
    gaugeColor = 'text-rose-400';
    gaugeBar = 'bg-rose-500';
    verdictTitle = 'ESCALATE — Critical Risk Detected';
    VerdictIcon = ShieldAlert;
  } else if (isRequestChanges) {
    bannerStyle = 'border-amber-500/40 bg-gradient-to-r from-amber-950/40 via-slate-900/70 to-slate-900/50';
    badgeStyle = 'bg-amber-500/20 text-amber-300 border-amber-500/40';
    gaugeColor = 'text-amber-400';
    gaugeBar = 'bg-amber-500';
    verdictTitle = 'REQUEST CHANGES — Action Required';
    VerdictIcon = AlertTriangle;
  }

  return (
    <div className={`border rounded-xl p-4 sm:p-5 shadow-lg relative overflow-hidden transition-all ${bannerStyle}`}>
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 relative z-10">
        {/* Left: Verdict Badge & Title & Summary */}
        <div className="flex-1 space-y-2.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className={`p-1.5 rounded-lg border ${badgeStyle}`}>
              <VerdictIcon className="w-5 h-5" />
            </div>
            <span className={`text-xs font-mono font-bold uppercase tracking-wider px-2.5 py-0.5 rounded-md border ${badgeStyle}`}>
              {normVerdict}
            </span>
            <h2 className="text-base sm:text-lg font-bold text-white tracking-tight">
              {verdictTitle}
            </h2>
            <span className="text-[11px] text-slate-400 flex items-center gap-1 font-mono ml-auto md:ml-0">
              <Sparkles className="w-3 h-3 text-indigo-400" /> Multi-Agent Consensus
            </span>
          </div>

          {/* Markdown Summary */}
          <div className="text-xs text-slate-300 prose prose-invert max-w-none leading-relaxed bg-slate-950/60 p-3 rounded-lg border border-slate-800/80">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {summary}
            </ReactMarkdown>
          </div>
        </div>

        {/* Right: Confidence Gauge & Action Buttons */}
        <div className="flex flex-row md:flex-col items-center justify-between md:justify-center gap-3 md:min-w-[170px] border-t md:border-t-0 md:border-l border-slate-800/80 pt-3 md:pt-0 md:pl-4">
          {/* Confidence Score Pill */}
          <div className="flex items-center md:flex-col justify-center gap-2 p-2.5 rounded-xl bg-slate-950/70 border border-slate-800/80 w-full text-center">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400">
              Confidence
            </span>
            <div className="flex items-baseline gap-1">
              <span className={`text-2xl font-extrabold font-mono leading-none ${gaugeColor}`}>
                {confidenceScore}
              </span>
              <span className="text-[10px] text-slate-500 font-mono">/ 100</span>
            </div>
            {/* Progress bar */}
            <div className="hidden md:block w-full bg-slate-800 h-1 rounded-full overflow-hidden mt-1">
              <div
                className={`h-full rounded-full transition-all duration-700 ${gaugeBar}`}
                style={{ width: `${Math.min(Math.max(confidenceScore, 5), 100)}%` }}
              />
            </div>
          </div>

          {/* Action Buttons */}
          <div className="flex gap-2 w-full">
            <button
              onClick={onReset}
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800/90 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-slate-700/80 transition-all cursor-pointer"
              title="Review another pull request diff"
            >
              <RefreshCw className="w-3 h-3" />
              <span>Reset</span>
            </button>
            <button
              onClick={onExportJSON}
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-semibold border border-indigo-500/30 transition-all cursor-pointer"
              title="Export structured JSON report"
            >
              <Download className="w-3 h-3" />
              <span>Export</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
