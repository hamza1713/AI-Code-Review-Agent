import React from 'react';
import { Info } from 'lucide-react';

interface ScopeDisclaimerProps {
  note: string;
}

export const ScopeDisclaimer: React.FC<ScopeDisclaimerProps> = ({ note }) => {
  return (
    <div className="p-2.5 rounded-xl bg-slate-900/40 border border-slate-800/80 text-[11px] text-slate-400 flex items-center gap-2">
      <Info className="w-3.5 h-3.5 text-indigo-400 shrink-0" />
      <p className="leading-snug text-slate-400">
        <span className="font-semibold text-slate-300 mr-1">Notice:</span>
        {note || 'Review performed via heuristic regex pattern scanning, AST Code Graph indexer (Python only), and governance rules engine.'}
      </p>
    </div>
  );
};
