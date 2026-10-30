import React from 'react';
import { Activity, Clock, Coins, Cpu, Hash } from 'lucide-react';
import type { TelemetryMetrics } from '../types/review';

interface TelemetryCardProps {
  telemetry?: TelemetryMetrics | null;
}

export const TelemetryCard: React.FC<TelemetryCardProps> = ({ telemetry }) => {
  if (!telemetry) return null;

  const duration = telemetry.duration_seconds != null ? `${telemetry.duration_seconds.toFixed(2)}s` : null;
  const usageAvailable = ['PROVIDER_REPORTED', 'ESTIMATED', 'MIXED'].includes(telemetry.token_usage_source ?? 'UNAVAILABLE');
  const totalTokens = usageAvailable ? telemetry.total_tokens.toLocaleString() : 'Unavailable';
  const promptTokens = usageAvailable ? telemetry.prompt_tokens.toLocaleString() : null;
  const completionTokens = usageAvailable ? telemetry.completion_tokens.toLocaleString() : null;
  const cost = (telemetry.estimated_cost_usd != null && telemetry.estimated_cost_usd > 0)
    ? `$${telemetry.estimated_cost_usd.toFixed(6)} USD`
    : null;
  const model = telemetry.model_used;

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-3 shadow-md">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-2 border-b border-slate-800/80">
        <div className="flex items-center space-x-2">
          <div className="w-5 h-5 rounded bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <Activity className="w-3 h-3" />
          </div>
          <h3 className="text-xs font-bold text-white tracking-tight uppercase">Execution Telemetry</h3>
        </div>
        {model && (
          <div className="flex items-center gap-1.5 text-[10px] font-mono text-pink-300 bg-pink-950/30 px-2 py-0.5 rounded border border-pink-900/40">
            <Cpu className="w-3 h-3 text-pink-400" />
            <span>{model.replace('gemini/', '')}</span>
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 mt-2">
        {/* Latency */}
        {duration && (
          <div className="p-1.5 px-2.5 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center gap-1.5">
            <Clock className="w-3 h-3 text-cyan-400" />
            <span className="text-[10px] text-slate-400">Latency:</span>
            <span className="text-xs font-bold font-mono text-white">{duration}</span>
          </div>
        )}

        {/* Total Tokens */}
        <div className="p-1.5 px-2.5 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center gap-1.5">
          <Hash className="w-3 h-3 text-indigo-400" />
          <span className="text-[10px] text-slate-400">Tokens:</span>
          <span className="text-xs font-bold font-mono text-indigo-300">{totalTokens}</span>
        </div>

        {/* In / Out (only when available) */}
        {promptTokens && completionTokens && (
          <div className="p-1.5 px-2.5 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center gap-1.5">
            <Hash className="w-3 h-3 text-purple-400" />
            <span className="text-[10px] text-slate-400">In / Out:</span>
            <span className="text-xs font-bold font-mono text-slate-300">
              {promptTokens} / {completionTokens}
            </span>
          </div>
        )}

        {/* Estimated Cost (only when available) */}
        {cost && (
          <div className="p-1.5 px-2.5 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center gap-1.5">
            <Coins className="w-3 h-3 text-emerald-400" />
            <span className="text-[10px] text-slate-400">Cost:</span>
            <span className="text-xs font-bold font-mono text-emerald-400">{cost}</span>
          </div>
        )}
      </div>
    </div>
  );
};
