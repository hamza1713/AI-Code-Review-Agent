import React, { useState, useEffect, useMemo } from 'react';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-json';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-sql';
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
  content: string;
  type: 'context' | 'added' | 'deleted';
  raw: string;
}

interface ParsedFile {
  filename: string;
  lines: ParsedFileLine[];
}

export const AnnotatedCodeViewer: React.FC<AnnotatedCodeViewerProps> = ({
  reviewedDiff,
  inlineComments = [],
}) => {

  const [selectedFile, setSelectedFile] = useState<string>('');
  const [activeFilter, setActiveFilter] = useState<'ALL' | 'CRITICAL' | 'WARNING' | 'INFO'>('ALL');
  const [expandedComments, setExpandedComments] = useState<Record<string, boolean>>({});
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [manuallyExpandedFolds, setManuallyExpandedFolds] = useState<Record<number, boolean>>({});

  // 1. Parse raw diff or plain file content into structured file lines
  const parsedFiles: ParsedFile[] = useMemo(() => {
    const raw = (reviewedDiff || '').trim();
    if (!raw) {
      // Fallback sample file if no raw diff is provided
      return [
        {
          filename: 'app/auth.py',
          lines: [
            { lineNumber: 1, content: 'import sqlite3', type: 'context', raw: 'import sqlite3' },
            { lineNumber: 2, content: 'from flask import request, jsonify', type: 'context', raw: 'from flask import request, jsonify' },
            { lineNumber: 3, content: '', type: 'context', raw: '' },
            { lineNumber: 4, content: 'def login_user():', type: 'context', raw: 'def login_user():' },
            { lineNumber: 5, content: '    username = request.form.get("username")', type: 'context', raw: '    username = request.form.get("username")' },
            { lineNumber: 6, content: '    password = request.form.get("password")', type: 'context', raw: '    password = request.form.get("password")' },
            { lineNumber: 7, content: '    conn = sqlite3.connect("users.db")', type: 'context', raw: '    conn = sqlite3.connect("users.db")' },
            { lineNumber: 8, content: '    query = f"SELECT * FROM users WHERE username = \'{username}\' AND password = \'{password}\'"', type: 'added', raw: '+    query = f"SELECT * FROM users WHERE username = \'{username}\' AND password = \'{password}\'"' },
            { lineNumber: 9, content: '    cursor = conn.cursor()', type: 'context', raw: '    cursor = conn.cursor()' },
            { lineNumber: 10, content: '    cursor.execute(query)', type: 'added', raw: '+    cursor.execute(query)' },
            { lineNumber: 11, content: '    return jsonify(cursor.fetchone())', type: 'context', raw: '    return jsonify(cursor.fetchone())' },
          ],
        },
      ];
    }

    // Check if it is a unified git diff format
    if (raw.includes('diff --git') || raw.includes('--- ') || raw.includes('@@ ')) {
      const files: ParsedFile[] = [];
      const diffSections = raw.split(/^diff --git /m);

      for (const section of diffSections) {
        if (!section.trim()) continue;

        let filename = 'modified_file.py';
        const fileMatch = section.match(/(?:b\/|a\/)([^\s\n\r]+)/);
        if (fileMatch) {
          filename = fileMatch[1];
        }

        const lines: ParsedFileLine[] = [];
        let currentLineNo = 1;

        const rawLines = section.split(/\r?\n/);
        for (const line of rawLines) {
          if (line.startsWith('@@')) {
            // Extract hunk line number: @@ -old,count +new,count @@
            const hunkMatch = line.match(/@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
            if (hunkMatch) {
              currentLineNo = parseInt(hunkMatch[1], 10);
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
            // Deleted line
            lines.push({
              lineNumber: currentLineNo,
              content: line.slice(1),
              type: 'deleted',
              raw: line,
            });
          } else if (line.startsWith(' ') || line === '') {
            lines.push({
              lineNumber: currentLineNo,
              content: line.startsWith(' ') ? line.slice(1) : line,
              type: 'context',
              raw: line,
            });
            currentLineNo++;
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

  // Set default selected file on mount
  useEffect(() => {
    if (parsedFiles.length > 0) {
      // Pick file with comments or first file
      const fileWithComments = parsedFiles.find(f =>
        inlineComments.some(c => c.path.includes(f.filename) || f.filename.includes(c.path))
      );
      setSelectedFile(fileWithComments ? fileWithComments.filename : parsedFiles[0].filename);
    }
  }, [parsedFiles, inlineComments]);

  // Active current file data
  const currentFile = useMemo(() => {
    return parsedFiles.find(f => f.filename === selectedFile) || parsedFiles[0] || { filename: '', lines: [] };
  }, [parsedFiles, selectedFile]);

  // Map comments by line number for current file
  const commentsByLine = useMemo(() => {
    const map: Record<number, InlineComment[]> = {};
    for (const comment of inlineComments) {
      const lineNo = comment.line;
      if (!map[lineNo]) {
        map[lineNo] = [];
      }
      map[lineNo].push(comment);
    }
    return map;
  }, [inlineComments]);

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
      const sev = (c.severity || 'INFO').toUpperCase();
      if (sev === 'CRITICAL') critical++;
      else if (sev === 'WARNING') warning++;
      else info++;
    }

    return { total: inlineComments.length, critical, warning, info };
  }, [inlineComments]);

  // 2. Auto-expand highest-severity / first comment on initial load
  useEffect(() => {
    if (inlineComments.length === 0) return;

    // Prioritize CRITICAL first, then WARNING, then first
    const criticalComment = inlineComments.find(c => (c.severity || '').toUpperCase() === 'CRITICAL');
    const warningComment = inlineComments.find(c => (c.severity || '').toUpperCase() === 'WARNING');
    const target = criticalComment || warningComment || inlineComments[0];

    if (target) {
      const key = `${target.path || selectedFile}-${target.line}-0`;
      setExpandedComments({ [key]: true });
    }
  }, [inlineComments, selectedFile]);

  const toggleComment = (key: string) => {
    setExpandedComments(prev => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  const handleCopyCode = (code: string, key: string) => {
    navigator.clipboard.writeText(code);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const expandAllComments = () => {
    const all: Record<string, boolean> = {};
    inlineComments.forEach((c, idx) => {
      all[`${c.path || selectedFile}-${c.line}-${idx}`] = true;
    });
    setExpandedComments(all);
  };

  const collapseAllComments = () => {
    setExpandedComments({});
  };

  // Syntax highlighter helper
  const highlightSyntax = (codeText: string) => {
    try {
      const lang = selectedFile.endsWith('.js') || selectedFile.endsWith('.ts')
        ? Prism.languages.javascript
        : selectedFile.endsWith('.json')
        ? Prism.languages.json
        : selectedFile.endsWith('.sql')
        ? Prism.languages.sql
        : Prism.languages.python;

      return Prism.highlight(codeText || ' ', lang || Prism.languages.clike, 'python');
    } catch {
      return codeText;
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
      const match = comments.some(c => (c.severity || '').toUpperCase() === activeFilter);
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
                  selectedFile === f.filename
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

      {/* 3. Interactive Code Canvas with Gutter Markers & In-Flow Cards */}
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

              const lineComments = commentsByLine[line.lineNumber] || [];
              const hasFlag = lineComments.length > 0;
              const topSeverity = hasFlag
                ? lineComments.some(c => (c.severity || '').toUpperCase() === 'CRITICAL')
                  ? 'CRITICAL'
                  : lineComments.some(c => (c.severity || '').toUpperCase() === 'WARNING')
                  ? 'WARNING'
                  : 'INFO'
                : null;

              const markerIndex = hasFlag ? flaggedLineNumbers.indexOf(line.lineNumber) : 0;
              const isAnyCardOpen = lineComments.some((_, cIdx) =>
                expandedComments[`${currentFile.filename}-${line.lineNumber}-${cIdx}`]
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
                  ? 'bg-rose-950/15 hover:bg-rose-950/25'
                  : topSeverity === 'WARNING'
                  ? 'bg-amber-950/15 hover:bg-amber-950/25'
                  : 'bg-sky-950/15 hover:bg-sky-950/25'
                : line.type === 'added'
                ? 'bg-emerald-950/20'
                : line.type === 'deleted'
                ? 'bg-rose-950/20 opacity-70'
                : 'hover:bg-slate-900/40';

              return (
                <React.Fragment key={`line-${line.lineNumber}-${idx}`}>
                  <tr
                    onClick={() => {
                      if (hasFlag) {
                        const key = `${currentFile.filename}-${line.lineNumber}-0`;
                        toggleComment(key);
                      }
                    }}
                    className={`transition-colors border-b border-slate-900/40 group ${rowBgClass} ${
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
                      {line.lineNumber}
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
                      const isOpen = expandedComments[cardKey];
                      if (!isOpen) return null;

                      const sev = (comment.severity || 'INFO').toUpperCase();
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
    </div>
  );
};
