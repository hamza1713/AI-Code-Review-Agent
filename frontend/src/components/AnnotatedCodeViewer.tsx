import React, { useState, useMemo } from 'react';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-jsx';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-tsx';
import 'prismjs/components/prism-json';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-sql';
import 'prismjs/components/prism-yaml';
import {
  ShieldAlert,
  AlertTriangle,
  Info,
  ChevronUp,
  Copy,
  Check,
  Code2,
  FileCode,
  Sparkles,
  Layers,
} from 'lucide-react';
import type { InlineComment } from '../types/review';

interface AnnotatedCodeViewerProps {
  reviewedDiff?: string | null;
  inlineComments: InlineComment[];
}

interface ParsedFileLine {
  lineNumber: number;
  oldLineNumber?: number;
  content: string;
  type: 'context' | 'added' | 'deleted';
  raw: string;
}

const escapeHtml = (text: string) =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

interface ParsedFile {
  filename: string;
  lines: ParsedFileLine[];
}

const normalizeSeverity = (sev?: string): 'CRITICAL' | 'WARNING' | 'INFO' => {
  const s = (sev || 'INFO').toUpperCase();
  if (s === 'CRITICAL' || s === 'HIGH' || s === 'BLOCKING') return 'CRITICAL';
  if (s === 'WARNING' || s === 'MEDIUM') return 'WARNING';
  return 'INFO';
};

  const normalizePath = (path: string) =>
    path.replace(/\\/g, '/').replace(/^\.?\//, '').replace(/^[ab]\//, '').toLowerCase();

  const pathsMatch = (commentPath: string, filePath: string) => {
    if (!commentPath || !filePath) return true;
    const cNorm = normalizePath(commentPath);
    const fNorm = normalizePath(filePath);
    if (cNorm === fNorm) return true;
    if (cNorm.endsWith('/' + fNorm) || fNorm.endsWith('/' + cNorm)) return true;
    const cBase = cNorm.split('/').pop();
    const fBase = fNorm.split('/').pop();
    return !!cBase && !!fBase && cBase === fBase;
  };

export const AnnotatedCodeViewer: React.FC<AnnotatedCodeViewerProps> = ({
  reviewedDiff,
  inlineComments = [],
}) => {

  const [selectedFile, setSelectedFile] = useState<string>('');
  const [activeFilter, setActiveFilter] = useState<'ALL' | 'CRITICAL' | 'WARNING' | 'INFO'>('ALL');
  const [expandedComments, setExpandedComments] = useState<Record<string, boolean>>({});
  const [copyError, setCopyError] = useState('');
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [manuallyExpandedFolds, setManuallyExpandedFolds] = useState<Record<number, boolean>>({});
  const [diffView, setDiffView] = useState<'unified' | 'split'>('unified');

  // 1. Parse raw diff or plain file content into structured file lines
  const parsedFiles: ParsedFile[] = useMemo(() => {
    const raw = (reviewedDiff || '').trim();
    if (!raw) return [];

    // Check if it is a unified git diff format
    if (raw.includes('diff --git') || raw.includes('--- ') || raw.includes('@@ ')) {
      const files: ParsedFile[] = [];
      const diffSections = raw.split(/(?=^diff --git |^--- (?:[ab]\/|\S+))/m);

      for (const section of diffSections) {
        if (!section.trim()) continue;

        let filename = 'modified_file.py';
        const fileMatch =
          section.match(/\+\+\+\s+(?:[b/]|"b\/)?([^\s\n\r]+)/) ||
          section.match(/---\s+(?:[a/]|"a\/)?([^\s\n\r]+)/) ||
          section.match(/diff --git\s+a\/([^\s]+)\s+b\/([^\s]+)/) ||
          section.match(/(?:b\/|a\/)([^\s\n\r]+)/);
        if (fileMatch) {
          filename = (fileMatch[2] || fileMatch[1]).replace(/^["']|["']$/g, '');
        }

        const lines: ParsedFileLine[] = [];
        let currentLineNo = 1;
        let oldLineNo = 1;

        const rawLines = section.split(/\r?\n/);
        for (const line of rawLines) {
          if (line.startsWith('@@')) {
            // Extract hunk line numbers: @@ -old,count +new,count @@
            const hunkMatch = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
            if (hunkMatch) {
              oldLineNo = parseInt(hunkMatch[1], 10);
              currentLineNo = parseInt(hunkMatch[2], 10);
            }
            continue;
          }
          if (line.startsWith('---') || line.startsWith('+++') || line.startsWith('index ') || line.startsWith('new file mode')) {
            continue;
          }

          if (line.startsWith('+')) {
            lines.push({
              lineNumber: currentLineNo,
              content: line.slice(1),
              type: 'added',
              raw: line,
            });
            currentLineNo++;
          } else if (line.startsWith('-')) {
            // Deleted line: never consumes or reuses a new-file line number
            lines.push({
              lineNumber: 0,
              oldLineNumber: oldLineNo,
              content: line.slice(1),
              type: 'deleted',
              raw: line,
            });
            oldLineNo++;
          } else if (line.startsWith(' ') || line === '') {
            lines.push({
              lineNumber: currentLineNo,
              oldLineNumber: oldLineNo,
              content: line.startsWith(' ') ? line.slice(1) : line,
              type: 'context',
              raw: line,
            });
            currentLineNo++;
            oldLineNo++;
          }
        }

        if (lines.length > 0) {
          files.push({ filename, lines });
        }
      }

      if (files.length > 0) return files;
    }

    // Plain source code representation
    const plainLines = raw.split(/\r?\n/).map((line, idx) => ({
      lineNumber: idx + 1,
      content: line,
      type: 'context' as const,
      raw: line,
    }));

    return [{ filename: 'source_file.py', lines: plainLines }];
  }, [reviewedDiff]);





  const currentFile = useMemo(() => {
    return parsedFiles.find(f => f.filename === selectedFile)
      || parsedFiles.find(f => inlineComments.some(c => pathsMatch(c.path, f.filename)))
      || parsedFiles[0] || { filename: '', lines: [] };
  }, [parsedFiles, selectedFile, inlineComments]);

  // Map comments by line number for current file
  const commentsByLine = useMemo(() => {
    const map: Record<number, InlineComment[]> = {};
    const validLines = new Set(currentFile.lines.map(l => l.lineNumber));

    for (const comment of inlineComments) {
      if (comment.side === 'LEFT') continue;
      if (!pathsMatch(comment.path, currentFile.filename)) continue;
      let lineNo = comment.line;

      // Fallback: If lineNo is not directly in rendered lines, but within hunk line index range:
      if (!validLines.has(lineNo) && currentFile.lines[lineNo - 1] && currentFile.lines[lineNo - 1].lineNumber > 0) {
        lineNo = currentFile.lines[lineNo - 1].lineNumber;
      }

      if (!map[lineNo]) {
        map[lineNo] = [];
      }
      map[lineNo].push(comment);
    }
    return map;
  }, [inlineComments, currentFile.filename, currentFile.lines]);

  // List of all flagged line numbers in the file
  const flaggedLineNumbers = useMemo(() => {
    return Object.keys(commentsByLine).map(Number).sort((a, b) => a - b);
  }, [commentsByLine]);

  // Count totals for summary strip
  const counts = useMemo(() => {
    let critical = 0;
    let warning = 0;
    let info = 0;

    for (const c of inlineComments) {
      const sev = normalizeSeverity(c.severity);
      if (sev === 'CRITICAL') critical++;
      else if (sev === 'WARNING') warning++;
      else info++;
    }

    return { total: inlineComments.length, critical, warning, info };
  }, [inlineComments]);

  const toggleComment = (key: string) => {
    setExpandedComments(prev => ({
      ...prev,
      [key]: prev[key] === undefined ? false : !prev[key],
    }));
  };

  const handleCopyCode = async (code: string, key: string) => {
    setCopyError('');
    try {
      await navigator.clipboard.writeText(code);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 2000);
    } catch {
      setCopiedKey(null);
      setCopyError('Copy failed. Select the text and copy it manually.');
    }
  };

  const expandAllComments = () => {
    const all: Record<string, boolean> = {};
    Object.entries(commentsByLine).forEach(([line, comments]) => {
      comments.forEach((_, index) => { all[`${currentFile.filename}-${line}-${index}`] = true; });
    });
    setExpandedComments(all);
  };

  const scrollToLine = (lineNo: number) => {
    const el = document.getElementById(`code-line-${lineNo}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      const key = `${currentFile.filename}-${lineNo}-0`;
      setExpandedComments(prev => ({ ...prev, [key]: true }));
    }
  };

  const collapseAllComments = () => {
    const all: Record<string, boolean> = {};
    Object.entries(commentsByLine).forEach(([line, comments]) => {
      comments.forEach((_, index) => { all[`${currentFile.filename}-${line}-${index}`] = false; });
    });
    setExpandedComments(all);
  };

  // Syntax highlighter helper
  const highlightSyntax = (codeText: string) => {
    try {
      const fn = currentFile.filename.toLowerCase();
      let lang = Prism.languages.python;
      let langName = 'python';

      if (fn.endsWith('.tsx')) {
        lang = Prism.languages.tsx || Prism.languages.typescript || Prism.languages.javascript;
        langName = 'tsx';
      } else if (fn.endsWith('.ts')) {
        lang = Prism.languages.typescript || Prism.languages.javascript;
        langName = 'typescript';
      } else if (fn.endsWith('.jsx')) {
        lang = Prism.languages.jsx || Prism.languages.javascript;
        langName = 'jsx';
      } else if (fn.endsWith('.js')) {
        lang = Prism.languages.javascript;
        langName = 'javascript';
      } else if (fn.endsWith('.json')) {
        lang = Prism.languages.json;
        langName = 'json';
      } else if (fn.endsWith('.sql')) {
        lang = Prism.languages.sql;
        langName = 'sql';
      } else if (fn.endsWith('.sh') || fn.endsWith('.bash')) {
        lang = Prism.languages.bash;
        langName = 'bash';
      } else if (fn.endsWith('.yaml') || fn.endsWith('.yml')) {
        lang = Prism.languages.yaml;
        langName = 'yaml';
      }

      return Prism.highlight(codeText || ' ', lang || Prism.languages.clike, langName);
    } catch {
      return escapeHtml(codeText);
    }
  };

  // Filtered lines calculation (Context ±2 lines when filtered)
  const visibleLineIndices = useMemo(() => {
    if (activeFilter === 'ALL' || flaggedLineNumbers.length === 0) {
      return new Set(currentFile.lines.map((_, idx) => idx));
    }

    const targetLines = new Set<number>();
    flaggedLineNumbers.forEach(lineNo => {
      const comments = commentsByLine[lineNo] || [];
      const match = comments.some(c => normalizeSeverity(c.severity) === activeFilter);
      if (match) {
        // Include lineNo ±2
        for (let i = lineNo - 2; i <= lineNo + 2; i++) {
          if (i >= 1 && i <= currentFile.lines.length) {
            targetLines.add(i);
          }
        }
      }
    });

    const indexSet = new Set<number>();
    currentFile.lines.forEach((line, idx) => {
      if (targetLines.has(line.lineNumber) || manuallyExpandedFolds[idx]) {
        indexSet.add(idx);
      }
    });

    return indexSet;
  }, [activeFilter, flaggedLineNumbers, commentsByLine, currentFile.lines, manuallyExpandedFolds]);

  return (
    <div className="bg-[#0e121a] border border-slate-800/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col">
      {copyError && <p role="alert" className="text-xs text-rose-300 mb-3">{copyError}</p>}
      {/* 1. Header Summary Strip */}
      <div className="p-4 bg-[#121622] border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shadow-inner">
            <Sparkles className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-slate-100 tracking-tight">
                Live Annotated Source Review
              </h2>
              <span className="text-[10px] font-mono bg-indigo-500/10 text-indigo-300 px-2 py-0.5 rounded-full border border-indigo-500/20">
                Pulsing Markers Active
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Pulsing gutter indicators & in-flow suggestion cards
            </p>
          </div>
        </div>

        {/* Severity Filter Pills */}
        <div className="flex flex-wrap items-center gap-1.5 bg-[#0a0c12] p-1 rounded-xl border border-slate-800/80">
          <button
            onClick={() => setActiveFilter('ALL')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeFilter === 'ALL'
                ? 'bg-slate-800 text-white shadow-sm border border-slate-700'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>All ({counts.total})</span>
          </button>

          <button
            onClick={() => setActiveFilter('CRITICAL')}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeFilter === 'CRITICAL'
                ? 'bg-rose-500/20 text-rose-300 border border-rose-500/50 shadow-sm'
                : 'text-slate-400 hover:text-rose-300'
            }`}
          >
            <span className="w-2 h-2 rounded-full bg-rose-500 pulse-dot-critical inline-block" />
            <span>Critical ({counts.critical})</span>
          </button>

          <button
            onClick={() => setActiveFilter('WARNING')}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeFilter === 'WARNING'
                ? 'bg-amber-500/20 text-amber-300 border border-amber-500/50 shadow-sm'
                : 'text-slate-400 hover:text-amber-300'
            }`}
          >
            <span className="w-2 h-2 rounded-full bg-amber-400 pulse-dot-warning inline-block" />
            <span>Warning ({counts.warning})</span>
          </button>

          <button
            onClick={() => setActiveFilter('INFO')}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeFilter === 'INFO'
                ? 'bg-sky-500/20 text-sky-300 border border-sky-500/50 shadow-sm'
                : 'text-slate-400 hover:text-sky-300'
            }`}
          >
            <span className="w-2 h-2 rounded-full bg-sky-400 pulse-dot-info inline-block" />
            <span>Info ({counts.info})</span>
          </button>
        </div>

        {/* Global Expand/Collapse Toggle */}
        <div className="flex items-center space-x-2">
          <div role="group" aria-label="Diff view mode" className="flex items-center bg-[#0a0c12] p-0.5 rounded-lg border border-slate-800/80">
            {(['unified', 'split'] as const).map(mode => (
              <button
                key={mode}
                aria-pressed={diffView === mode}
                onClick={() => setDiffView(mode)}
                className={`text-[11px] px-2.5 py-1 rounded-md capitalize cursor-pointer ${
                  diffView === mode ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
          <button
            onClick={expandAllComments}
            className="text-[11px] text-slate-400 hover:text-slate-200 px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-800 hover:border-slate-700 transition-colors cursor-pointer"
          >
            Expand All
          </button>
          <button
            onClick={collapseAllComments}
            className="text-[11px] text-slate-400 hover:text-slate-200 px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-800 hover:border-slate-700 transition-colors cursor-pointer"
          >
            Collapse All
          </button>
        </div>
      </div>

      {/* 2. File Tabs / Selector */}
      {parsedFiles.length > 1 && (
        <div className="px-4 py-2 bg-[#0a0c10] border-b border-slate-800/80 flex items-center space-x-2 overflow-x-auto">
          <FileCode className="w-3.5 h-3.5 text-slate-500 shrink-0" />
          <span className="text-xs text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Files:</span>
          {parsedFiles.map(f => {
            const hasIssues = inlineComments.some(c => c.path.includes(f.filename) || f.filename.includes(c.path));
            return (
              <button
                key={f.filename}
                onClick={() => setSelectedFile(f.filename)}
                className={`text-xs font-mono px-3 py-1 rounded-lg transition-all flex items-center space-x-1.5 cursor-pointer ${
                  currentFile.filename === f.filename
                    ? 'bg-indigo-600/30 text-indigo-200 border border-indigo-500/40 shadow-sm'
                    : 'text-slate-400 hover:text-slate-200 bg-slate-900/60 border border-transparent'
                }`}
              >
                <span>{f.filename}</span>
                {hasIssues && (
                  <span className="w-1.5 h-1.5 rounded-full bg-rose-500 pulse-dot-critical inline-block" />
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* 3. Code Canvas with Minimap Rail */}
      <div className="relative flex bg-[#07090e] border-t border-slate-800/80">
        <div className="flex-1 overflow-x-auto font-mono text-xs selection:bg-indigo-600/40">
      {diffView === 'split' && (
        <div className="overflow-x-auto bg-[#07090e] font-mono text-xs">
          <table className="w-full border-collapse table-fixed">
            <tbody>
              {(() => {
                type Row = { left?: ParsedFileLine; right?: ParsedFileLine };
                const rows: Row[] = [];
                let pending: ParsedFileLine[] = [];
                for (const l of currentFile.lines) {
                  if (l.type === 'deleted') { pending.push(l); continue; }
                  if (l.type === 'added') {
                    const del = pending.shift();
                    rows.push({ left: del, right: l });
                    continue;
                  }
                  pending.forEach(d => rows.push({ left: d }));
                  pending = [];
                  rows.push({ left: l, right: l });
                }
                pending.forEach(d => rows.push({ left: d }));
                return rows.map((row, i) => {
                  const rightComments = row.right ? (commentsByLine[row.right.lineNumber] || []) : [];
                  const flagged = rightComments.length > 0;
                  return (
                    <React.Fragment key={`split-${i}`}>
                      <tr id={row.right && row.right.lineNumber > 0 ? `code-line-${row.right.lineNumber}` : undefined} className="border-b border-slate-900/40">
                        <td className={`w-10 px-2 py-1 text-right text-slate-600 align-top ${row.left?.type === 'deleted' ? 'bg-rose-950/30' : ''}`}>
                          {row.left ? (row.left.oldLineNumber ?? '') : ''}
                        </td>
                        <td className={`px-2 py-1 whitespace-pre overflow-hidden align-top ${row.left?.type === 'deleted' ? 'bg-rose-950/30 text-rose-200' : 'text-slate-300'}`}
                          dangerouslySetInnerHTML={{ __html: row.left ? highlightSyntax(row.left.content) : '' }} />
                        <td className={`w-10 px-2 py-1 text-right text-slate-600 align-top border-l border-slate-800 ${row.right?.type === 'added' ? 'bg-emerald-950/30' : ''}`}>
                          {row.right ? row.right.lineNumber : ''}
                        </td>
                        <td className={`px-2 py-1 whitespace-pre overflow-hidden align-top ${flagged ? 'bg-rose-950/20 border-l-4 border-l-rose-500' : row.right?.type === 'added' ? 'bg-emerald-950/30' : ''} text-slate-200`}
                          dangerouslySetInnerHTML={{ __html: row.right ? highlightSyntax(row.right.content) : '' }} />
                      </tr>
                      {rightComments.map((c, cIdx) => {
                        const cardKey = `split-${currentFile.filename}-${row.right?.lineNumber}-${cIdx}`;
                        const sev = normalizeSeverity(c.severity);
                        return (
                          <tr key={`split-card-${i}-${cIdx}`} className="bg-[#0a0d14] border-b border-slate-800/90">
                            <td colSpan={4} className="p-3">
                              <div className="bg-[#131826] border border-slate-800 rounded-xl p-4 shadow-xl space-y-3">
                                <div className="flex items-center justify-between">
                                  <div className="flex items-center space-x-2.5">
                                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border uppercase tracking-wider ${
                                      sev === 'CRITICAL' ? 'bg-rose-500/20 text-rose-300 border-rose-500/40' :
                                      sev === 'WARNING' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' :
                                      'bg-sky-500/20 text-sky-300 border-sky-500/40'
                                    }`}>
                                      {sev}
                                    </span>
                                    <span className="text-xs font-semibold text-slate-100">{c.comment_body}</span>
                                  </div>
                                </div>
                                {c.why && (
                                  <div className="p-2.5 rounded-lg bg-indigo-950/30 border border-indigo-500/20 text-indigo-200 text-[11px] flex items-start space-x-2">
                                    <span className="text-indigo-400 font-bold shrink-0">💡 Why:</span>
                                    <span className="leading-relaxed">{c.why}</span>
                                  </div>
                                )}
                                {c.suggestion_code && (
                                  <div className="rounded-lg bg-[#07090e] border border-slate-800/90 overflow-hidden text-xs">
                                    <div className="px-3 py-1.5 bg-[#0e121a] border-b border-slate-800 flex items-center justify-between">
                                      <div className="flex items-center space-x-2">
                                        <Code2 className="w-3 h-3 text-emerald-400" />
                                        <span className="text-[10px] font-semibold text-slate-300 uppercase tracking-wider">
                                          Suggested Fix (Proposed Patch)
                                        </span>
                                      </div>
                                      <button
                                        onClick={e => {
                                          e.stopPropagation();
                                          handleCopyCode(c.suggestion_code || '', cardKey);
                                        }}
                                        className="text-[10px] text-slate-300 hover:text-white flex items-center gap-1 px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 transition-colors cursor-pointer"
                                      >
                                        {copiedKey === cardKey ? (
                                          <>
                                            <Check className="w-3 h-3 text-emerald-400" />
                                            <span>Copied!</span>
                                          </>
                                        ) : (
                                          <>
                                            <Copy className="w-3 h-3" />
                                            <span>Copy Code</span>
                                          </>
                                        )}
                                      </button>
                                    </div>
                                    <div className="px-3 py-1.5 bg-rose-950/20 border-b border-rose-900/30 text-rose-300 font-mono text-[11px] flex items-center space-x-2">
                                      <span className="select-none text-rose-500 font-bold">-</span>
                                      <span className="line-through opacity-80">{row.right?.content?.trim() || '(original)'}</span>
                                    </div>
                                    <div className="px-3 py-1.5 bg-emerald-950/20 text-emerald-300 font-mono text-[11px] flex items-start space-x-2">
                                      <span className="select-none text-emerald-400 font-bold">+</span>
                                      <pre className="whitespace-pre overflow-x-auto flex-1 font-mono">{c.suggestion_code}</pre>
                                    </div>
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </React.Fragment>
                  );
                });
              })()}
            </tbody>
          </table>
        </div>
      )}

      {/* 3. Interactive Code Canvas with Gutter Markers & In-Flow Cards */}
      {diffView === 'unified' && (
      <div className="overflow-x-auto bg-[#07090e] font-mono text-xs selection:bg-indigo-600/40">
        <table className="w-full border-collapse">
          <tbody>
            {currentFile.lines.map((line, idx) => {
              const isVisible = visibleLineIndices.has(idx);
              const isPrevVisible = idx === 0 || visibleLineIndices.has(idx - 1);

              // Check if we need to render a folded line separator
              if (!isVisible) {
                if (isPrevVisible) {
                  return (
                    <tr key={`fold-${idx}`} className="bg-[#0b0e14]/60 border-y border-slate-900">
                      <td colSpan={3} className="py-1 px-4 text-center">
                        <button
                          onClick={() => setManuallyExpandedFolds(prev => ({ ...prev, [idx]: true }))}
                          className="text-[10px] text-slate-500 hover:text-indigo-400 font-sans tracking-wide cursor-pointer py-0.5 px-3 rounded hover:bg-slate-900 transition-colors"
                        >
                          ··· Hidden context lines (click to reveal) ···
                        </button>
                      </td>
                    </tr>
                  );
                }
                return null;
              }

              const lineComments = line.type === 'deleted' ? [] : (commentsByLine[line.lineNumber] || []);
              const hasFlag = lineComments.length > 0;
              const topSeverity = hasFlag
                ? lineComments.some(c => normalizeSeverity(c.severity) === 'CRITICAL')
                  ? 'CRITICAL'
                  : lineComments.some(c => normalizeSeverity(c.severity) === 'WARNING')
                  ? 'WARNING'
                  : 'INFO'
                : null;

              const markerIndex = hasFlag ? flaggedLineNumbers.indexOf(line.lineNumber) : 0;
              const isAnyCardOpen = lineComments.some((_, cIdx) =>
                expandedComments[`${currentFile.filename}-${line.lineNumber}-${cIdx}`] ?? true
              );

              const wavyClass = topSeverity === 'CRITICAL'
                ? 'wavy-critical'
                : topSeverity === 'WARNING'
                ? 'wavy-warning'
                : topSeverity === 'INFO'
                ? 'wavy-info'
                : '';

              const rowBgClass = hasFlag
                ? topSeverity === 'CRITICAL'
                  ? 'bg-rose-950/20 hover:bg-rose-950/30'
                  : topSeverity === 'WARNING'
                  ? 'bg-amber-950/20 hover:bg-amber-950/30'
                  : 'bg-sky-950/20 hover:bg-sky-950/30'
                : line.type === 'added'
                ? 'bg-emerald-950/20'
                : line.type === 'deleted'
                ? 'bg-rose-950/20 opacity-70'
                : 'hover:bg-slate-900/40';

              const borderLeftClass = hasFlag
                ? topSeverity === 'CRITICAL'
                  ? 'border-l-4 border-l-rose-500'
                  : topSeverity === 'WARNING'
                  ? 'border-l-4 border-l-amber-500'
                  : 'border-l-4 border-l-sky-500'
                : '';

              return (
                <React.Fragment key={`line-${line.lineNumber}-${idx}`}>
                  <tr
                    id={line.lineNumber > 0 ? `code-line-${line.lineNumber}` : undefined}
                    onClick={() => {
                      if (hasFlag) {
                        lineComments.forEach((_, cIdx) => {
                          const key = `${currentFile.filename}-${line.lineNumber}-${cIdx}`;
                          toggleComment(key);
                        });
                      }
                    }}
                    className={`transition-colors border-b border-slate-900/40 group ${rowBgClass} ${borderLeftClass} ${
                      hasFlag ? 'cursor-pointer' : ''
                    }`}
                  >
                    {/* Gutter: Pulsing Marker & Status */}
                    <td className="w-10 px-2 py-1 text-center select-none shrink-0 align-top">
                      {hasFlag ? (
                        <div
                          className="inline-flex items-center justify-center animate-stagger-marker"
                          style={{ animationDelay: `${markerIndex * 80}ms` }}
                          title={`Line ${line.lineNumber}: ${lineComments.length} Issue(s) found (Click to view)`}
                        >
                          {topSeverity === 'CRITICAL' ? (
                            <span className="w-2.5 h-2.5 rounded-full bg-rose-500 pulse-dot-critical shadow-md" />
                          ) : topSeverity === 'WARNING' ? (
                            <span className="w-2.5 h-2.5 rounded-full bg-amber-400 pulse-dot-warning shadow-md" />
                          ) : (
                            <span className="w-2.5 h-2.5 rounded-full bg-sky-400 pulse-dot-info shadow-md" />
                          )}
                        </div>
                      ) : (
                        <span className="w-2 h-2 inline-block opacity-0">.</span>
                      )}
                    </td>

                    {/* Gutter: Line Number */}
                    <td className="w-12 px-2 py-1 text-right select-none text-slate-600 font-mono text-[11px] border-r border-slate-800/80 shrink-0 align-top group-hover:text-slate-400">
                      {line.type === 'deleted' ? `−${line.oldLineNumber ?? ''}` : line.lineNumber}
                    </td>

                    {/* Code Content with Wavy Decoration */}
                    <td className="px-3 py-1 text-slate-200 whitespace-pre overflow-x-auto align-top">
                      <div className="flex items-center justify-between">
                        <span
                          className={`inline-block ${wavyClass}`}
                          dangerouslySetInnerHTML={{
                            __html: highlightSyntax(line.content),
                          }}
                        />
                        {hasFlag && (
                          <span className="text-[10px] text-slate-500 font-sans tracking-wide opacity-0 group-hover:opacity-100 transition-opacity ml-2 shrink-0">
                            {isAnyCardOpen ? '▼ Hide Card' : '▶ Show Card'}
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>

                  {/* 4. In-Flow Click-to-Expand Comment Card */}
                  {hasFlag &&
                    lineComments.map((comment, cIdx) => {
                      const cardKey = `${currentFile.filename}-${line.lineNumber}-${cIdx}`;
                      const isOpen = expandedComments[cardKey] ?? true;
                      if (!isOpen) return null;

                      const sev = normalizeSeverity(comment.severity);
                      const sevBadgeClass =
                        sev === 'CRITICAL'
                          ? 'bg-rose-500/20 text-rose-300 border-rose-500/40'
                          : sev === 'WARNING'
                          ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                          : 'bg-sky-500/20 text-sky-300 border-sky-500/40';

                      return (
                        <tr key={`card-${cardKey}`} className="bg-[#0a0d14] border-b border-slate-800/90">
                          <td colSpan={3} className="p-3 pl-12 pr-4">
                            <div className="bg-[#131826] border border-slate-800 rounded-xl p-4 shadow-xl space-y-3">
                              {/* Card Header */}
                              <div className="flex items-center justify-between">
                                <div className="flex items-center space-x-2.5">
                                  {sev === 'CRITICAL' ? (
                                    <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
                                  ) : sev === 'WARNING' ? (
                                    <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
                                  ) : (
                                    <Info className="w-4 h-4 text-sky-400 shrink-0" />
                                  )}

                                  <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border uppercase tracking-wider ${sevBadgeClass}`}>
                                    {sev}
                                  </span>

                                  <span className="text-xs font-semibold text-slate-100">
                                    {comment.comment_body}
                                  </span>
                                </div>

                                <button
                                  onClick={e => {
                                    e.stopPropagation();
                                    toggleComment(cardKey);
                                  }}
                                  className="text-slate-400 hover:text-slate-200 text-xs px-2 py-0.5 rounded bg-slate-900 hover:bg-slate-800 transition-colors flex items-center gap-1 cursor-pointer"
                                >
                                  <ChevronUp className="w-3 h-3" />
                                  <span>Close</span>
                                </button>
                              </div>

                              {/* Why Explanation (Causal Mechanism) */}
                              {comment.why && (
                                <div className="p-2.5 rounded-lg bg-indigo-950/30 border border-indigo-500/20 text-indigo-200 text-[11px] flex items-start space-x-2">
                                  <span className="text-indigo-400 font-bold shrink-0">💡 Why:</span>
                                  <span className="leading-relaxed">{comment.why}</span>
                                </div>
                              )}

                              {/* 1-Click Diff / Suggestion Block */}
                              {comment.suggestion_code && (
                                <div className="rounded-lg bg-[#07090e] border border-slate-800/90 overflow-hidden text-xs">
                                  <div className="px-3 py-1.5 bg-[#0e121a] border-b border-slate-800 flex items-center justify-between">
                                    <div className="flex items-center space-x-2">
                                      <Code2 className="w-3 h-3 text-emerald-400" />
                                      <span className="text-[10px] font-semibold text-slate-300 uppercase tracking-wider">
                                        Suggested Fix (1-Click Diff)
                                      </span>
                                    </div>
                                    <button
                                      onClick={e => {
                                        e.stopPropagation();
                                        handleCopyCode(comment.suggestion_code || '', cardKey);
                                      }}
                                      className="text-[10px] text-slate-300 hover:text-white flex items-center gap-1 px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 transition-colors cursor-pointer"
                                    >
                                      {copiedKey === cardKey ? (
                                        <>
                                          <Check className="w-3 h-3 text-emerald-400" />
                                          <span>Copied!</span>
                                        </>
                                      ) : (
                                        <>
                                          <Copy className="w-3 h-3" />
                                          <span>Copy Code</span>
                                        </>
                                      )}
                                    </button>
                                  </div>

                                  {/* Old Line Diff Highlight */}
                                  <div className="px-3 py-1.5 bg-rose-950/20 border-b border-rose-900/30 text-rose-300 font-mono text-[11px] flex items-center space-x-2">
                                    <span className="select-none text-rose-500 font-bold">-</span>
                                    <span className="line-through opacity-80">{line.content.trim() || '(empty line)'}</span>
                                  </div>

                                  {/* New Replacement Diff Highlight */}
                                  <div className="p-3 bg-emerald-950/20 text-emerald-300 font-mono text-[11px] flex items-start space-x-2">
                                    <span className="select-none text-emerald-400 font-bold shrink-0">+</span>
                                    <pre className="overflow-x-auto leading-relaxed whitespace-pre-wrap">
                                      <code>{comment.suggestion_code.trim()}</code>
                                    </pre>
                                  </div>
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      )}
        </div>

        {/* Minimap Rail */}
        <aside
          aria-label="Diff navigation minimap"
          className="w-7 bg-[#05070a] border-l border-slate-800/80 sticky top-0 h-auto min-h-[300px] select-none py-2 hidden sm:flex flex-col items-center"
        >
          <div className="text-[8px] font-mono text-slate-500 mb-1 font-bold" title="Code findings map">MAP</div>
          <div className="relative w-full flex-1 min-h-[260px]">
            {flaggedLineNumbers.map((lineNo) => {
              const comments = commentsByLine[lineNo] || [];
              const topSev = comments.some(c => normalizeSeverity(c.severity) === 'CRITICAL')
                ? 'CRITICAL'
                : comments.some(c => normalizeSeverity(c.severity) === 'WARNING')
                ? 'WARNING'
                : 'INFO';
              const totalLines = Math.max(1, currentFile.lines.length);
              const topPercent = Math.min(96, Math.max(2, (lineNo / totalLines) * 100));
              return (
                <button
                  key={`minimap-${lineNo}`}
                  onClick={() => scrollToLine(lineNo)}
                  title={`Jump to Line ${lineNo} (${topSev}): ${comments[0]?.comment_body || ''}`}
                  style={{ top: `${topPercent}%` }}
                  className={`absolute left-1/2 -translate-x-1/2 w-3.5 h-1.5 rounded-sm transition-transform hover:scale-150 cursor-pointer ${
                    topSev === 'CRITICAL'
                      ? 'bg-rose-500 shadow-sm shadow-rose-500/80'
                      : topSev === 'WARNING'
                      ? 'bg-amber-400 shadow-sm shadow-amber-400/80'
                      : 'bg-sky-400 shadow-sm shadow-sky-400/80'
                  }`}
                />
              );
            })}
          </div>
        </aside>
      </div>
    </div>
  );
};
