import React from 'react';
import { Activity, Clock, Coins, Cpu, Hash } from 'lucide-react';
import type { TelemetryMetrics } from '../types/review';

interface TelemetryCardProps {
  telemetry?: TelemetryMetrics | null;
}

export const TelemetryCard: React.FC<TelemetryCardProps> = ({ telemetry }) => {
  const duration = telemetry?.duration_seconds ? `${telemetry.duration_seconds.toFixed(2)}s` : '0.00s';
  const totalTokens = telemetry?.total_tokens ? telemetry.total_tokens.toLocaleString() : '0';
  const promptTokens = telemetry?.prompt_tokens ? telemetry.prompt_tokens.toLocaleString() : '0';
  const completionTokens = telemetry?.completion_tokens ? telemetry.completion_tokens.toLocaleString() : '0';
  const cost = telemetry?.estimated_cost_usd !== undefined
    ? `$${telemetry.estimated_cost_usd.toFixed(6)} USD`
    : '$0.000000 USD';
  const model = telemetry?.model_used || 'gemini-3.1-flash-lite-preview';

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-3.5 shadow-md">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-2.5 border-b border-slate-800/80">
        <div className="flex items-center space-x-2">
          <div className="w-6 h-6 rounded bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <Activity className="w-3.5 h-3.5" />
          </div>
          <h3 className="text-xs font-bold text-white tracking-tight uppercase">Execution & Cost Telemetry</h3>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] font-mono text-pink-300 bg-pink-950/30 px-2 py-0.5 rounded border border-pink-900/40">
          <Cpu className="w-3 h-3 text-pink-400" />
          <span>{model.replace('gemini/', '')}</span>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 mt-2.5">
        {/* Latency */}
        <div className="p-2 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-slate-400 text-[10px]">
            <Clock className="w-3 h-3 text-cyan-400" />
            <span>Latency</span>
          </div>
          <span className="text-xs font-bold font-mono text-white">
            {duration}
          </span>
        </div>

        {/* Total Tokens */}
        <div className="p-2 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-slate-400 text-[10px]">
            <Hash className="w-3 h-3 text-indigo-400" />
            <span>Tokens</span>
          </div>
          <span className="text-xs font-bold font-mono text-indigo-300">
            {totalTokens}
          </span>
        </div>

        {/* Breakdown */}
        <div className="p-2 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-slate-400 text-[10px]">
            <Hash className="w-3 h-3 text-purple-400" />
            <span>In / Out</span>
          </div>
          <span className="text-[10px] font-bold font-mono text-slate-300">
            {promptTokens} / {completionTokens}
          </span>
        </div>

        {/* Estimated Cost */}
        <div className="p-2 rounded-lg bg-slate-900/70 border border-slate-800/80 flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-slate-400 text-[10px]">
            <Coins className="w-3 h-3 text-emerald-400" />
            <span>Cost</span>
          </div>
          <span className="text-xs font-bold font-mono text-emerald-400">
            {cost}
          </span>
        </div>
      </div>
    </div>
  );
};
