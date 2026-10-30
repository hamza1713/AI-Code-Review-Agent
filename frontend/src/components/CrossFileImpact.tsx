import React, { useState, useMemo } from 'react';
import {
  GitGraph,
  CornerDownRight,
  CheckCircle2,
  ZoomIn,
  ZoomOut,
  RotateCcw,
  Copy,
  Check,
  Code2,
  Network,
  ListTree,
} from 'lucide-react';
import type { CrossFileImpactSummary } from '../types/review';

interface CrossFileImpactProps {
  impact: CrossFileImpactSummary;
}

type ViewMode = 'canvas' | 'mermaid' | 'tree';

export const CrossFileImpact: React.FC<CrossFileImpactProps> = ({ impact }) => {
  const [viewMode, setViewMode] = useState<ViewMode>('canvas');
  const [zoom, setZoom] = useState<number>(1);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [copiedMermaid, setCopiedMermaid] = useState<boolean>(false);

  const callersMap = useMemo(() => impact.impacted_callers || {}, [impact.impacted_callers]);
  const modifiedFunctions = useMemo(() => Object.keys(callersMap), [callersMap]);
  const hasCallers = modifiedFunctions.length > 0;

  // Compute total unique callers
  const allCallers = useMemo(() => {
    const set = new Set<string>();
    Object.values(callersMap).forEach(list => list.forEach(c => set.add(c)));
    return Array.from(set);
  }, [callersMap]);

  // Generate Mermaid diagram string
  const mermaidDiagram = useMemo(() => {
    if (!hasCallers) return 'flowchart LR\n  NoChanges["No cross-file caller dependencies modified"]';
    let code = 'flowchart LR\n';
    code += '  classDef mod fill:#0e7490,stroke:#06b6d4,stroke-width:2px,color:#ffffff;\n';
    code += '  classDef caller fill:#312e81,stroke:#6366f1,stroke-width:2px,color:#ffffff;\n';
    code += '  classDef sink fill:#881337,stroke:#f43f5e,stroke-width:2px,color:#ffffff;\n\n';

    modifiedFunctions.forEach((func, idx) => {
      const modId = `Mod_${idx}`;
      code += `  ${modId}["⚡ ${func}"]:::mod\n`;
      const callers = callersMap[func] || [];
      if (callers.length === 0) {
        code += `  Isolated_${idx}["(Self-contained Scope)"] --> ${modId}\n`;
      } else {
        callers.forEach((c, cIdx) => {
          const cId = `Caller_${idx}_${cIdx}`;
          code += `  ${cId}["📌 ${c}"]:::caller --> ${modId}\n`;
        });
      }
    });

    return code;
  }, [hasCallers, modifiedFunctions, callersMap]);

  const handleCopyMermaid = async () => {
    try {
      await navigator.clipboard.writeText(mermaidDiagram);
      setCopiedMermaid(true);
      setTimeout(() => setCopiedMermaid(false), 2000);
    } catch {
      setCopiedMermaid(false);
    }
  };

  // Node layout coordinates for SVG Canvas
  const canvasNodes = useMemo(() => {
    const nodes: Array<{
      id: string;
      label: string;
      type: 'caller' | 'modified' | 'sink';
      x: number;
      y: number;
      connectedTo: string[];
    }> = [];

    const startXCallers = 60;
    const startXModified = 360;

    // Modified Functions (Center)
    modifiedFunctions.forEach((func, idx) => {
      const y = 80 + idx * 90;
      nodes.push({
        id: `mod-${func}`,
        label: func,
        type: 'modified',
        x: startXModified,
        y,
        connectedTo: [],
      });
    });

    // Callers (Left)
    let callerIndex = 0;
    modifiedFunctions.forEach((func) => {
      const callers = callersMap[func] || [];
      callers.forEach((caller) => {
        const existing = nodes.find(n => n.id === `caller-${caller}`);
        if (existing) {
          existing.connectedTo.push(`mod-${func}`);
        } else {
          const y = 60 + callerIndex * 65;
          nodes.push({
            id: `caller-${caller}`,
            label: caller,
            type: 'caller',
            x: startXCallers,
            y,
            connectedTo: [`mod-${func}`],
          });
          callerIndex++;
        }
      });
    });

    return nodes;
  }, [modifiedFunctions, callersMap]);

  const svgHeight = Math.max(340, Math.max(modifiedFunctions.length, allCallers.length) * 80 + 80);

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between pb-3 border-b border-slate-800/80 gap-2">
        <div className="flex items-center space-x-2.5">
          <div className="w-8 h-8 rounded-lg bg-cyan-500/10 border border-cyan-500/20 flex items-center justify-center text-cyan-400">
            <GitGraph className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-bold text-white tracking-tight uppercase">
                Cross-File Blast Radius & Taint Flow
              </h3>
              <span className={`text-[9px] uppercase font-mono font-bold px-1.5 py-0.5 rounded-full border ${
                impact.is_python
                  ? 'bg-cyan-500/10 text-cyan-300 border-cyan-500/20'
                  : 'bg-slate-800 text-slate-400 border-slate-700'
              }`}>
                {impact.is_python ? 'Polyglot AST' : 'Syntactic Graph'}
              </span>
            </div>
            <p className="text-[10px] text-slate-400">
              Inter-procedural call chains & dataflow impact ({allCallers.length} caller(s) affected)
            </p>
          </div>
        </div>

        {/* View Switcher Controls */}
        <div className="flex items-center space-x-1.5 bg-[#0a0c10] p-0.5 rounded-lg border border-slate-800/80">
          <button
            onClick={() => setViewMode('canvas')}
            className={`flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              viewMode === 'canvas' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'
            }`}
            title="Interactive Visual Node-Edge Canvas"
          >
            <Network className="w-3 h-3" />
            <span>Canvas</span>
          </button>
          <button
            onClick={() => setViewMode('mermaid')}
            className={`flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              viewMode === 'mermaid' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'
            }`}
            title="Mermaid Sequence / Flowchart for PR comments"
          >
            <Code2 className="w-3 h-3" />
            <span>Mermaid</span>
          </button>
          <button
            onClick={() => setViewMode('tree')}
            className={`flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              viewMode === 'tree' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'
            }`}
            title="Hierarchical Caller List"
          >
            <ListTree className="w-3 h-3" />
            <span>Tree</span>
          </button>
        </div>
      </div>

      {/* 1. Interactive Canvas View */}
      {viewMode === 'canvas' && (
        <div className="mt-3 relative bg-[#07090e] border border-slate-800/80 rounded-xl overflow-hidden">
          {/* Canvas Floating Toolbar */}
          <div className="absolute top-3 right-3 z-10 flex items-center space-x-1 bg-slate-900/90 border border-slate-800 p-1 rounded-lg backdrop-blur shadow-md">
            <button
              onClick={() => setZoom(z => Math.min(1.6, z + 0.15))}
              className="p-1 text-slate-400 hover:text-white rounded hover:bg-slate-800 transition-colors"
              title="Zoom In"
            >
              <ZoomIn className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => setZoom(z => Math.max(0.6, z - 0.15))}
              className="p-1 text-slate-400 hover:text-white rounded hover:bg-slate-800 transition-colors"
              title="Zoom Out"
            >
              <ZoomOut className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => { setZoom(1); setSelectedNode(null); }}
              className="p-1 text-slate-400 hover:text-white rounded hover:bg-slate-800 transition-colors"
              title="Reset View"
            >
              <RotateCcw className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* SVG Graph Canvas */}
          <div className="overflow-auto max-h-[380px] p-2">
            <svg
              width={760 * zoom}
              height={svgHeight * zoom}
              viewBox={`0 0 760 ${svgHeight}`}
              className="transition-transform duration-200 ease-out select-none"
            >
              <defs>
                <marker
                  id="arrow"
                  viewBox="0 0 10 10"
                  refX="10"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#06b6d4" />
                </marker>
                <marker
                  id="arrow-active"
                  viewBox="0 0 10 10"
                  refX="10"
                  refY="5"
                  markerWidth="7"
                  markerHeight="7"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
                </marker>
              </defs>

              {/* Connecting Edges */}
              {canvasNodes.map((node) => {
                if (node.type !== 'caller') return null;
                return node.connectedTo.map((targetId) => {
                  const target = canvasNodes.find(n => n.id === targetId);
                  if (!target) return null;
                  const isSelected = selectedNode === node.id || selectedNode === target.id;
                  const strokeColor = isSelected ? '#38bdf8' : '#334155';
                  const strokeWidth = isSelected ? 2.5 : 1.5;

                  // Cubic bezier curve connector
                  const startX = node.x + 160;
                  const startY = node.y + 20;
                  const endX = target.x;
                  const endY = target.y + 20;
                  const dx = (endX - startX) / 2;
                  const pathD = `M ${startX} ${startY} C ${startX + dx} ${startY}, ${endX - dx} ${endY}, ${endX} ${endY}`;

                  return (
                    <path
                      key={`edge-${node.id}-${target.id}`}
                      d={pathD}
                      fill="none"
                      stroke={strokeColor}
                      strokeWidth={strokeWidth}
                      strokeDasharray={isSelected ? 'none' : '4 2'}
                      markerEnd={isSelected ? 'url(#arrow-active)' : 'url(#arrow)'}
                      className="transition-all duration-300"
                    />
                  );
                });
              })}

              {/* Nodes */}
              {canvasNodes.map((node) => {
                const isSelected = selectedNode === node.id;
                const isModified = node.type === 'modified';

                const rectWidth = isModified ? 180 : 160;
                const rectHeight = 40;

                const bgFill = isModified
                  ? isSelected ? '#0e7490' : '#155e75'
                  : isSelected ? '#3730a3' : '#1e1b4b';

                const borderStroke = isModified
                  ? isSelected ? '#22d3ee' : '#06b6d4'
                  : isSelected ? '#818cf8' : '#4338ca';

                return (
                  <g
                    key={node.id}
                    transform={`translate(${node.x}, ${node.y})`}
                    onClick={() => setSelectedNode(selectedNode === node.id ? null : node.id)}
                    className="cursor-pointer group"
                  >
                    <rect
                      width={rectWidth}
                      height={rectHeight}
                      rx={8}
                      fill={bgFill}
                      stroke={borderStroke}
                      strokeWidth={isSelected ? 2 : 1}
                      className="transition-all duration-200 group-hover:brightness-125"
                    />
                    <text
                      x={12}
                      y={18}
                      fill={isModified ? '#a5f3fc' : '#c7d2fe'}
                      fontSize={10}
                      fontWeight="bold"
                      fontFamily="monospace"
                    >
                      {isModified ? '⚡ MODIFIED' : '📌 CALLER'}
                    </text>
                    <text
                      x={12}
                      y={31}
                      fill="#ffffff"
                      fontSize={11}
                      fontFamily="monospace"
                      fontWeight="600"
                    >
                      {node.label.length > 20 ? `${node.label.slice(0, 18)}…` : node.label}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>

          {/* Canvas Footer Legend */}
          <div className="p-2.5 bg-[#0a0d14] border-t border-slate-800/80 flex flex-wrap items-center justify-between text-[10px] text-slate-400 font-mono">
            <div className="flex items-center space-x-3">
              <span className="flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-indigo-500" />
                Upstream Caller (Blast Radius)
              </span>
              <span className="flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-cyan-400" />
                Modified Symbol
              </span>
            </div>
            <span>Click node to isolate path</span>
          </div>
        </div>
      )}

      {/* 2. Mermaid Architecture View */}
      {viewMode === 'mermaid' && (
        <div className="mt-3 space-y-2">
          <div className="flex items-center justify-between bg-slate-900/60 p-2.5 rounded-lg border border-slate-800">
            <span className="text-[11px] font-mono text-slate-300">
              Mermaid Diagram Definition (Ready for GitHub/GitLab PR Comment):
            </span>
            <button
              onClick={handleCopyMermaid}
              className="flex items-center gap-1 text-[11px] text-slate-200 bg-slate-800 hover:bg-slate-700 px-2.5 py-1 rounded transition-colors cursor-pointer"
            >
              {copiedMermaid ? (
                <>
                  <Check className="w-3 h-3 text-emerald-400" />
                  <span className="text-emerald-300 font-semibold">Copied!</span>
                </>
              ) : (
                <>
                  <Copy className="w-3 h-3" />
                  <span>Copy Mermaid</span>
                </>
              )}
            </button>
          </div>
          <pre className="p-3 rounded-lg bg-[#07090e] border border-slate-800 text-[11px] font-mono text-cyan-300 overflow-x-auto leading-relaxed max-h-[300px]">
            {mermaidDiagram}
          </pre>
        </div>
      )}

      {/* 3. Tree View */}
      {viewMode === 'tree' && (
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
            <div className="py-4 px-3 text-center border border-dashed border-slate-800 rounded-lg bg-slate-900/30">
              <div className="flex items-center justify-center gap-2 text-xs text-cyan-400 font-medium">
                <CheckCircle2 className="w-4 h-4" />
                <span>No cross-file caller dependencies affected.</span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
