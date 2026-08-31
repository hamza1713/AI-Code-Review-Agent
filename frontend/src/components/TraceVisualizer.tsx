import React, { useState } from 'react';
import type { TraceData, TraceNode } from '../types/review';


interface TraceVisualizerProps {
  trace?: TraceData | null;
}

export const TraceVisualizer: React.FC<TraceVisualizerProps> = ({ trace }) => {
  const [expandedNodes, setExpandedNodes] = useState<Record<string, boolean>>({});

  if (!trace || !trace.nodes || trace.nodes.length === 0) {
    return null;
  }

  const toggleNode = (nodeId: string) => {
    setExpandedNodes(prev => ({ ...prev, [nodeId]: !prev[nodeId] }));
  };

  const getStageBadgeColor = (stage: string) => {
    switch (stage) {
      case 'INGESTION':
        return 'bg-blue-900/60 text-blue-300 border-blue-700';
      case 'SECURITY_SCAN':
        return 'bg-red-900/60 text-red-300 border-red-700';
      case 'GOVERNANCE':
        return 'bg-purple-900/60 text-purple-300 border-purple-700';
      case 'AGENT_CREW':
        return 'bg-amber-900/60 text-amber-300 border-amber-700';
      case 'SYNTHESIS':
        return 'bg-emerald-900/60 text-emerald-300 border-emerald-700';
      default:
        return 'bg-slate-800 text-slate-300 border-slate-700';
    }
  };

  const renderNode = (node: TraceNode, depth: number = 0) => {
    const isExpanded = expandedNodes[node.id] !== false; // Default expanded
    const hasChildren = node.children && node.children.length > 0;
    const hasDetails = node.details && Object.keys(node.details).length > 0;

    return (
      <div key={node.id} className="relative mb-2" style={{ marginLeft: `${depth * 20}px` }}>
        {depth > 0 && (
          <div className="absolute -left-3 top-4 w-3 h-px bg-slate-700" />
        )}
        <div className="bg-slate-900/80 border border-slate-800 rounded-lg p-3 hover:border-slate-700 transition-colors">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              {hasChildren && (
                <button
                  onClick={() => toggleNode(node.id)}
                  className="text-slate-400 hover:text-slate-200 text-xs font-mono w-4 h-4 flex items-center justify-center bg-slate-800 rounded"
                >
                  {isExpanded ? '▼' : '▶'}
                </button>
              )}
              <span className={`px-2 py-0.5 text-xs font-medium rounded border ${getStageBadgeColor(node.stage)}`}>
                {node.stage}
              </span>
              <span className="text-sm font-semibold text-slate-200">{node.title}</span>
              {node.agent_name && (
                <span className="text-xs text-indigo-400 bg-indigo-950/40 px-2 py-0.5 rounded border border-indigo-900">
                  🤖 {node.agent_name}
                </span>
              )}
            </div>
            <div className="flex items-center space-x-3 text-xs">
              {node.duration_ms !== undefined && node.duration_ms !== null && (
                <span className="text-slate-400 font-mono">⚡ {node.duration_ms}ms</span>
              )}
              <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-950/60 text-emerald-300 border border-emerald-800">
                {node.status}
              </span>
            </div>
          </div>

          {hasDetails && (
            <div className="mt-2 text-xs bg-slate-950/70 rounded p-2 border border-slate-800/80 text-slate-400 font-mono overflow-x-auto">
              <pre>{JSON.stringify(node.details, null, 2)}</pre>
            </div>
          )}
        </div>

        {hasChildren && isExpanded && (
          <div className="mt-2 border-l border-slate-800 pl-2">
            {node.children!.map(child => renderNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-lg mt-6">
      <div className="flex items-center justify-between pb-4 mb-4 border-b border-slate-800">
        <div className="flex items-center space-x-2">
          <span className="text-lg">🌳</span>
          <h3 className="text-base font-semibold text-slate-100">Multi-Agent Execution Trace</h3>
          <span className="text-xs bg-slate-800 text-slate-300 px-2 py-0.5 rounded-full font-mono">
            {trace.trace_id}
          </span>
        </div>
        <div className="text-xs text-slate-400 font-mono">
          Total Duration: <span className="text-indigo-400 font-semibold">{trace.duration_ms}ms</span>
        </div>
      </div>

      <div className="space-y-2">
        {trace.nodes.map(node => renderNode(node, 0))}
      </div>
    </div>
  );
};
