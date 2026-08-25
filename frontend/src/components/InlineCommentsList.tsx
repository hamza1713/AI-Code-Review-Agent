import React, { useState } from 'react';
import { MessageSquareCode, Copy, Check, GitCommit, CheckCircle2 } from 'lucide-react';
import type { InlineComment } from '../types/review';

interface InlineCommentsListProps {
  comments: InlineComment[];
}

export const InlineCommentsList: React.FC<InlineCommentsListProps> = ({ comments }) => {
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  const handleCopySuggestion = (code: string, idx: number) => {
    navigator.clipboard.writeText(code);
    setCopiedIndex(idx);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const getSeverityBadge = (sev: string) => {
    const s = (sev || 'SUGGESTION').toUpperCase();
    if (s === 'CRITICAL') {
      return 'bg-rose-500/20 text-rose-300 border-rose-500/40';
    }
    if (s === 'WARNING') {
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    }
    return 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30';
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between pb-3 border-b border-slate-800/80">
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <MessageSquareCode className="w-3.5 h-3.5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-bold text-white tracking-tight uppercase">Line-Level Inline Suggestions</h3>
              <span className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono font-bold ${
                comments.length > 0
                  ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30'
                  : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
              }`}>
                {comments.length}
              </span>
            </div>
            <p className="text-[10px] text-slate-400">GitHub 1-click suggestion diff blocks</p>
          </div>
        </div>
      </div>

      {/* List */}
      <div className="mt-3 space-y-2.5 max-h-[380px] overflow-y-auto pr-1 text-xs">
        {comments.length === 0 ? (
          <div className="py-4 px-3 text-center border border-dashed border-slate-800 rounded-lg bg-slate-900/30">
            <div className="flex items-center justify-center gap-2 text-xs text-emerald-400 font-medium">
              <CheckCircle2 className="w-4 h-4" />
              <span>No line-level comments generated. Clean diff!</span>
            </div>
          </div>
        ) : (
          comments.map((comment, idx) => (
            <div
              key={idx}
              className="bg-slate-900/70 border border-slate-800/80 rounded-lg p-3 space-y-2 hover:border-slate-700 transition-all"
            >
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <span className={`text-[9px] uppercase font-mono font-bold px-1.5 py-0.5 rounded border ${getSeverityBadge(comment.severity)}`}>
                  {comment.severity || 'SUGGESTION'}
                </span>

                <div className="text-[10px] font-mono text-indigo-300 bg-indigo-950/40 px-1.5 py-0.5 rounded border border-indigo-900/40">
                  {comment.path}:L{comment.line} ({comment.side || 'RIGHT'})
                </div>
              </div>

              <p className="text-slate-300 text-[11px] leading-relaxed">
                {comment.comment_body}
              </p>

              {comment.suggestion_code && (
                <div className="rounded-lg bg-[#0a0c10] border border-slate-800/80 overflow-hidden">
                  <div className="px-2.5 py-1 bg-slate-900 border-b border-slate-800 flex items-center justify-between">
                    <span className="text-[9px] font-mono text-slate-400 flex items-center gap-1">
                      <GitCommit className="w-2.5 h-2.5 text-indigo-400" />
                      1-Click Suggestion Diff
                    </span>
                    <button
                      onClick={() => handleCopySuggestion(comment.suggestion_code || '', idx)}
                      className="text-[9px] text-slate-300 hover:text-white flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 transition-colors cursor-pointer"
                      title="Copy suggestion code"
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
                  <pre className="p-2.5 text-[10px] font-mono text-emerald-300 bg-emerald-950/10 overflow-x-auto leading-relaxed max-h-[160px]">
                    <code>{comment.suggestion_code.trim()}</code>
                  </pre>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};
