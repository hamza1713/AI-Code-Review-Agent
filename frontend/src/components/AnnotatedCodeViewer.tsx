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
  ArrowRight,
} from 'lucide-react';
import type { InlineComment, SastFinding, RuleViolation } from '../types/review';

export interface AnnotatedCodeViewerProps {
  reviewedDiff?: string | null;
  inlineComments?: InlineComment[];
  findings?: SastFinding[];
  governanceViolations?: RuleViolation[];
}

export interface UnifiedAnnotation {
  id: string;
  filePath: string;
  line: number;
  severity: 'CRITICAL' | 'WARNING' | 'INFO';
  badgeTitle: string;
  ruleId?: string;
  cwe?: string;
  sourceType: 'SECURITY' | 'GOVERNANCE' | 'INLINE';
  commentBody: string;
  whyExplanation: string;
  suggestionCode?: string;
  originalSnippet?: string;
  isRedFlag: boolean;
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
  path.replace(/\\/g, '/').replace(/^\.?\//, '').replace(/^[ab]\//, '').toLowerCase().trim();

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
  findings = [],
  governanceViolations = [],
}) => {
  const [selectedFile, setSelectedFile] = useState<string>('');
  const [activeFilter, setActiveFilter] = useState<'ALL' | 'CRITICAL' | 'WARNING' | 'INFO'>('ALL');
  const [expandedComments, setExpandedComments] = useState<Record<string, boolean>>({});
  const [copyError, setCopyError] = useState('');
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [manuallyExpandedFolds, setManuallyExpandedFolds] = useState<Record<number, boolean>>({});
  const [diffView, setDiffView] = useState<'unified' | 'split'>('unified');

  // 1. Synthesize unified annotations from findings (SAST), governance, and inline comments
  const unifiedAnnotations = useMemo<UnifiedAnnotation[]>(() => {
    const result: UnifiedAnnotation[] = [];
    const seen = new Set<string>();

    // A. Security findings (Red flags)
    for (let idx = 0; idx < findings.length; idx++) {
      const f = findings[idx];
      const fPath = f.file_path || '';
      const line = f.line_number && f.line_number > 0 ? f.line_number : 1;
      const sev = normalizeSeverity(f.severity);
      const key = `finding-${f.rule_id || idx}-${normalizePath(fPath)}-${line}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const isSql = (f.rule_id && f.rule_id.includes('SQLI')) || (f.cwe && f.cwe.includes('89'));
      const isAuth = (f.rule_id && f.rule_id.includes('AUTH')) || (f.cwe && f.cwe.includes('256'));

      let why = '';
      if (isSql) {
        why =
          'Formatted string (f-string) interpolation used directly in SQL query execution. This creates a critical SQL Injection (CWE-89) vulnerability, allowing an attacker to inject arbitrary SQL statements (e.g., "\' OR \'1\'=\'1\' --") to bypass authentication, dump sensitive database tables, or execute unauthorized database commands.';
      } else if (isAuth) {
        why =
          'Plaintext password comparison detected. Passwords must be verified via cryptographic hashes (e.g., bcrypt/argon2) with constant-time comparison to prevent credential exposure in memory dumps or timing attacks.';
      } else if (f.description && f.description.length > 20) {
        why = `${f.description} This suspicious pattern introduces an exploitable security vulnerability (${f.cwe || f.rule_id || 'CWE'}) and must be remediated.`;
      } else {
        why = `Critical defect detected by security scanner (${f.rule_id || 'VULN'}${f.cwe ? ` • ${f.cwe}` : ''}). Potential vulnerability requires parameterized handling or sanitization.`;
      }

      let suggCode = f.fix_recommendation || '';
      const codeMatch = suggCode.match(/`([^`]+)`/);
      if (codeMatch && !suggCode.includes('\n')) {
        suggCode = codeMatch[1];
      } else if (suggCode.includes('```')) {
        const blockMatch = suggCode.match(/```(?:[a-zA-Z]*\n)?([\s\S]*?)```/);
        if (blockMatch) suggCode = blockMatch[1].trim();
      }

      result.push({
        id: key,
        filePath: fPath,
        line,
        severity: sev,
        badgeTitle:
          sev === 'CRITICAL'
            ? `🚨 RED FLAG: ${f.rule_id || 'SECURITY'}${f.cwe ? ` • ${f.cwe}` : ''}`
            : `⚠️ SECURITY FINDING: ${f.rule_id || 'SECURITY'}${f.cwe ? ` • ${f.cwe}` : ''}`,
        ruleId: f.rule_id,
        cwe: f.cwe,
        sourceType: 'SECURITY',
        commentBody: f.description || `Security defect detected: ${f.rule_id || 'Vulnerability'}`,
        whyExplanation: why,
        suggestionCode: suggCode,
        originalSnippet: f.snippet,
        isRedFlag: sev === 'CRITICAL',
      });
    }

    // B. Governance rule violations
    for (let idx = 0; idx < governanceViolations.length; idx++) {
      const g = governanceViolations[idx];
      const gPath = g.file_path || '';
      const line = g.line_number && g.line_number > 0 ? g.line_number : 1;
      const sev = normalizeSeverity(g.severity);
      const key = `gov-${g.rule_id || idx}-${normalizePath(gPath)}-${line}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const isPrint =
        (g.rule_id && g.rule_id.includes('PRINT')) ||
        (g.rule_name && g.rule_name.toLowerCase().includes('print'));

      let why = '';
      if (isPrint) {
        why =
          'Prohibited print() statement in production code: Direct stdout logging causes unstructured logs, bypasses centralized log monitoring and level filtering, and introduces performance I/O bottlenecks. Use a structured logger (e.g., logger.info or logger.error) instead.';
      } else if (g.description && g.description.length > 20) {
        why = `${g.description} This violates team engineering policies and must be refactored prior to merge.`;
      } else {
        why = `Violates repository governance policy: ${g.rule_name || g.rule_id}.`;
      }

      result.push({
        id: key,
        filePath: gPath,
        line,
        severity: sev,
        badgeTitle:
          sev === 'CRITICAL'
            ? `🚨 BLOCKING GOVERNANCE: ${g.rule_name || g.rule_id}`
            : `⚠️ GOVERNANCE VIOLATION: ${g.rule_name || g.rule_id}`,
        ruleId: g.rule_id,
        sourceType: 'GOVERNANCE',
        commentBody: g.description || `Policy violation: ${g.rule_name || g.rule_id}`,
        whyExplanation: why,
        suggestionCode: g.suggested_fix,
        originalSnippet: g.snippet,
        isRedFlag: sev === 'CRITICAL',
      });
    }

    // C. Inline suggestions
    for (let idx = 0; idx < inlineComments.length; idx++) {
      const c = inlineComments[idx];
      if (c.side === 'LEFT') continue;
      const cPath = c.path || '';
      const line = c.line && c.line > 0 ? c.line : 1;
      const sev = normalizeSeverity(c.severity);
      const key = `inline-${idx}-${normalizePath(cPath)}-${line}`;

      const alreadyExists = result.some(
        r => pathsMatch(r.filePath, cPath) && r.line === line
      );
      if (alreadyExists) continue;

      result.push({
        id: key,
        filePath: cPath,
        line,
        severity: sev,
        badgeTitle:
          sev === 'CRITICAL'
            ? `🚨 RED FLAG: ${c.comment_body.slice(0, 40)}`
            : `💬 INLINE SUGGESTION: Line ${line}`,
        sourceType: 'INLINE',
        commentBody: c.comment_body,
        whyExplanation:
          c.why || 'Identified during automated code review for quality and compliance.',
        suggestionCode: c.suggestion_code,
        isRedFlag: sev === 'CRITICAL',
      });
    }

    return result;
  }, [findings, governanceViolations, inlineComments]);

  // 2. Parse raw diff or plain file content into structured file lines
  const parsedFiles: ParsedFile[] = useMemo(() => {
    const raw = (reviewedDiff || '').trim();
    if (!raw) return [];

    // Unified git diff format
    if (raw.includes('diff --git') || raw.includes('--- ') || raw.includes('@@ ')) {
      const files: ParsedFile[] = [];
      const diffSections = raw.includes('diff --git')
        ? raw.split(/(?=^diff --git )/m)
        : raw.split(/(?=^--- (?:[ab]\/|\S+))/m);

      for (const section of diffSections) {
        if (!section.trim()) continue;

        let filename = 'modified_file.py';
        const fileMatch =
          section.match(/^diff --git\s+(?:a\/)?([^\s]+)\s+(?:b\/)?([^\s]+)/m) ||
          section.match(/^\+\+\+\s+(?:b\/)?"?([^"\s\r\n]+)"?/m) ||
          section.match(/^---\s+(?:a\/)?"?([^"\s\r\n]+)"?/m) ||
          section.match(/(?:b\/|a\/)([^\s\n\r]+)/);
        if (fileMatch) {
          filename = normalizePath(fileMatch[2] || fileMatch[1]).replace(/^["']|["']$/g, '');
        }

        const lines: ParsedFileLine[] = [];
        let currentLineNo = 1;
        let oldLineNo = 1;

        const rawLines = section.split(/\r?\n/);
        for (const line of rawLines) {
          if (line.startsWith('@@')) {
            const hunkMatch = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
            if (hunkMatch) {
              oldLineNo = parseInt(hunkMatch[1], 10);
              currentLineNo = parseInt(hunkMatch[2], 10);
            }
            continue;
          }
          if (
            line.startsWith('diff --git') ||
            line.startsWith('---') ||
            line.startsWith('+++') ||
            line.startsWith('index ') ||
            line.startsWith('new file mode')
          ) {
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
          const existing = files.find(f => f.filename === filename);
          if (existing) {
            existing.lines.push(...lines);
          } else {
            files.push({ filename, lines });
          }
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

  // Current selected file (auto-picks file with annotations if not explicitly selected)
  const currentFile = useMemo(() => {
    if (selectedFile) {
      const found = parsedFiles.find(f => f.filename === selectedFile);
      if (found) return found;
    }
    const withAnnotations = parsedFiles.find(f =>
      unifiedAnnotations.some(a => pathsMatch(a.filePath, f.filename))
    );
    if (withAnnotations) return withAnnotations;

    return parsedFiles[0] || { filename: '', lines: [] };
  }, [parsedFiles, selectedFile, unifiedAnnotations]);

  // Map annotations by line number for current file
  const annotationsByLine = useMemo(() => {
    const map: Record<number, UnifiedAnnotation[]> = {};
    const validLines = new Set(currentFile.lines.map(l => l.lineNumber));

    for (const ann of unifiedAnnotations) {
      if (!pathsMatch(ann.filePath, currentFile.filename)) continue;
      let lineNo = ann.line;

      // Fallback: If lineNo is not directly in rendered lines, find nearest valid line
      if (!validLines.has(lineNo) && currentFile.lines[lineNo - 1] && currentFile.lines[lineNo - 1].lineNumber > 0) {
        lineNo = currentFile.lines[lineNo - 1].lineNumber;
      }

      if (!map[lineNo]) {
        map[lineNo] = [];
      }
      map[lineNo].push(ann);
    }
    return map;
  }, [unifiedAnnotations, currentFile.filename, currentFile.lines]);

  // List of all flagged line numbers in the file
  const flaggedLineNumbers = useMemo(() => {
    return Object.keys(annotationsByLine).map(Number).sort((a, b) => a - b);
  }, [annotationsByLine]);

  // Summary counts
  const counts = useMemo(() => {
    let critical = 0;
    let warning = 0;
    let info = 0;

    for (const a of unifiedAnnotations) {
      if (a.severity === 'CRITICAL') critical++;
      else if (a.severity === 'WARNING') warning++;
      else info++;
    }

    return { total: unifiedAnnotations.length, critical, warning, info };
  }, [unifiedAnnotations]);

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
    Object.entries(annotationsByLine).forEach(([line, anns]) => {
      anns.forEach((_, index) => { all[`${currentFile.filename}-${line}-${index}`] = true; });
    });
    setExpandedComments(all);
  };

  const collapseAllComments = () => {
    const all: Record<string, boolean> = {};
    Object.entries(annotationsByLine).forEach(([line, anns]) => {
      anns.forEach((_, index) => { all[`${currentFile.filename}-${line}-${index}`] = false; });
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

  // Filtered line indices calculation
  const visibleLineIndices = useMemo(() => {
    if (activeFilter === 'ALL' || flaggedLineNumbers.length === 0) {
      return new Set(currentFile.lines.map((_, idx) => idx));
    }

    const targetLines = new Set<number>();
    flaggedLineNumbers.forEach(lineNo => {
      const anns = annotationsByLine[lineNo] || [];
      const match = anns.some(a => normalizeSeverity(a.severity) === activeFilter);
      if (match) {
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
  }, [activeFilter, flaggedLineNumbers, annotationsByLine, currentFile.lines, manuallyExpandedFolds]);

  return (
    <div className="bg-[#0e121a] border border-slate-800/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col">
      {copyError && <p role="alert" className="text-xs text-rose-300 p-2">{copyError}</p>}

      {/* 1. Header Summary Strip */}
      <div className="p-4 bg-[#121622] border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-rose-500/20 via-indigo-500/20 to-purple-500/20 border border-rose-500/30 flex items-center justify-center text-rose-400 shadow-inner">
            <Sparkles className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-slate-100 tracking-tight">
                Live Annotated Source Review
              </h2>
              <span className="text-[10px] font-mono bg-rose-500/10 text-rose-300 px-2 py-0.5 rounded-full border border-rose-500/30">
                Pulsing Markers Active
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Interactive red flags, vulnerability rationale &amp; 1-click secure diffs
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
            <span>Critical / Red Flag ({counts.critical})</span>
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

        {/* Global Expand/Collapse Toggle & Diff Mode */}
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
            const hasIssues = unifiedAnnotations.some(a => pathsMatch(a.filePath, f.filename));
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

      {/* 3. Quick-Jump Strip to Suspicious / Flagged Lines */}
      {flaggedLineNumbers.length > 0 && (
        <div className="px-4 py-2.5 bg-[#090c13] border-b border-slate-800/80 flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-bold text-slate-400 flex items-center gap-1.5 uppercase tracking-wider">
            <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
            <span>Flagged Suspicious Lines:</span>
          </span>
          {flaggedLineNumbers.map((lineNo) => {
            const anns = annotationsByLine[lineNo] || [];
            const isCrit = anns.some(a => a.isRedFlag || a.severity === 'CRITICAL');
            const first = anns[0];
            const label = first?.ruleId || (isCrit ? 'Red Flag' : 'Warning');
            return (
              <button
                key={`jump-${lineNo}`}
                onClick={() => scrollToLine(lineNo)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-mono font-semibold transition-all cursor-pointer shadow-sm ${
                  isCrit
                    ? 'bg-rose-500/20 hover:bg-rose-500/30 text-rose-200 border border-rose-500/50'
                    : 'bg-amber-500/20 hover:bg-amber-500/30 text-amber-200 border border-amber-500/50'
                }`}
              >
                <span>{isCrit ? '🚨' : '⚠️'}</span>
                <span>Line {lineNo}</span>
                <span className="text-[11px] font-sans opacity-90 truncate max-w-[180px]">
                  ({label})
                </span>
                <ArrowRight className="w-3 h-3 opacity-60 ml-0.5" />
              </button>
            );
          })}
        </div>
      )}

      {/* 4. Code Canvas with Minimap Rail */}
      <div className="relative flex bg-[#07090e]">
        <div className="flex-1 overflow-x-auto font-mono text-xs selection:bg-indigo-600/40">

          {/* Split Diff View */}
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
                      const rightAnns = row.right ? (annotationsByLine[row.right.lineNumber] || []) : [];
                      const flagged = rightAnns.length > 0;
                      const isCrit = rightAnns.some(a => a.isRedFlag || a.severity === 'CRITICAL');
                      const isWarn = !isCrit && rightAnns.some(a => a.severity === 'WARNING');

                      const splitRowBg = flagged
                        ? isCrit
                          ? 'suspicious-line-critical'
                          : isWarn
                          ? 'suspicious-line-warning'
                          : 'bg-sky-950/20'
                        : row.right?.type === 'added'
                        ? 'bg-emerald-950/30'
                        : '';

                      return (
                        <React.Fragment key={`split-${i}`}>
                          <tr
                            id={row.right && row.right.lineNumber > 0 ? `code-line-${row.right.lineNumber}` : undefined}
                            className="border-b border-slate-900/40"
                          >
                            <td className={`w-10 px-2 py-1 text-right text-slate-600 align-top ${row.left?.type === 'deleted' ? 'bg-rose-950/30' : ''}`}>
                              {row.left ? (row.left.oldLineNumber ?? '') : ''}
                            </td>
                            <td
                              className={`px-2 py-1 whitespace-pre overflow-hidden align-top ${row.left?.type === 'deleted' ? 'bg-rose-950/30 text-rose-200' : 'text-slate-300'}`}
                              dangerouslySetInnerHTML={{ __html: row.left ? highlightSyntax(row.left.content) : '' }}
                            />
                            <td className={`w-10 px-2 py-1 text-right text-slate-600 align-top border-l border-slate-800 ${row.right?.type === 'added' ? 'bg-emerald-950/30' : ''}`}>
                              {row.right ? row.right.lineNumber : ''}
                            </td>
                            <td
                              className={`px-2 py-1 whitespace-pre overflow-hidden align-top ${splitRowBg} ${flagged ? (isCrit ? 'border-l-4 border-l-rose-500' : 'border-l-4 border-l-amber-500') : ''} text-slate-200`}
                              dangerouslySetInnerHTML={{ __html: row.right ? highlightSyntax(row.right.content) : '' }}
                            />
                          </tr>

                          {rightAnns.map((ann, cIdx) => {
                            const cardKey = `split-${currentFile.filename}-${row.right?.lineNumber}-${cIdx}`;
                            const isCritCard = ann.isRedFlag || ann.severity === 'CRITICAL';
                            const isWarnCard = !isCritCard && ann.severity === 'WARNING';
                            return (
                              <tr key={`split-card-${i}-${cIdx}`} className="bg-[#080b12] border-b border-slate-800/90">
                                <td colSpan={4} className="p-3">
                                  <div className={`border rounded-2xl p-4 shadow-2xl space-y-3 ${
                                    isCritCard
                                      ? 'bg-[#120d14] border-rose-500/40'
                                      : isWarnCard
                                      ? 'bg-[#14120a] border-amber-500/40'
                                      : 'bg-[#101422] border-indigo-500/30'
                                  }`}>
                                    <div className="flex items-center justify-between">
                                      <div className="flex items-center space-x-2.5">
                                        <span className={`text-[10px] font-black px-2.5 py-0.5 rounded-full border uppercase tracking-wider ${
                                          isCritCard
                                            ? 'bg-rose-500/20 text-rose-300 border-rose-500/40'
                                            : isWarnCard
                                            ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                                            : 'bg-sky-500/20 text-sky-300 border-sky-500/40'
                                        }`}>
                                          {ann.badgeTitle}
                                        </span>
                                        <span className="text-xs font-semibold text-slate-100">{ann.commentBody}</span>
                                      </div>
                                    </div>

                                    {/* Why Callout */}
                                    <div className={`p-3 rounded-xl border flex items-start space-x-2.5 ${
                                      isCritCard
                                        ? 'bg-rose-950/30 border-rose-500/30 text-rose-100'
                                        : 'bg-amber-950/30 border-amber-500/30 text-amber-100'
                                    }`}>
                                      <span className="text-base select-none shrink-0 mt-0.5">💡</span>
                                      <div className="space-y-1">
                                        <div className={`text-[11px] font-bold uppercase tracking-wider ${
                                          isCritCard ? 'text-rose-400' : 'text-amber-400'
                                        }`}>
                                          Why this line is suspicious &amp; a red flag:
                                        </div>
                                        <p className="text-xs leading-relaxed text-slate-200 font-sans">
                                          {ann.whyExplanation}
                                        </p>
                                      </div>
                                    </div>

                                    {/* Fix Recommendation */}
                                    {ann.suggestionCode && (
                                      <div className="rounded-xl bg-[#07090e] border border-slate-800/90 overflow-hidden text-xs">
                                        <div className="px-3.5 py-2 bg-[#0d1017] border-b border-slate-800 flex items-center justify-between">
                                          <div className="flex items-center space-x-2">
                                            <Code2 className="w-3.5 h-3.5 text-emerald-400" />
                                            <span className="text-[10px] font-bold text-slate-300 uppercase tracking-wider">
                                              Proposed Secure Patch
                                            </span>
                                          </div>
                                          <button
                                            onClick={e => {
                                              e.stopPropagation();
                                              handleCopyCode(ann.suggestionCode || '', cardKey);
                                            }}
                                            className="text-[10px] text-slate-300 hover:text-white flex items-center gap-1 px-2.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 transition-colors cursor-pointer"
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
                                          <pre className="whitespace-pre overflow-x-auto flex-1 font-mono">{ann.suggestionCode.trim()}</pre>
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

          {/* Unified Diff View */}
          {diffView === 'unified' && (
            <div className="overflow-x-auto bg-[#07090e] font-mono text-xs selection:bg-indigo-600/40">
              <table className="w-full border-collapse">
                <tbody>
                  {currentFile.lines.map((line, idx) => {
                    const isVisible = visibleLineIndices.has(idx);
                    const isPrevVisible = idx === 0 || visibleLineIndices.has(idx - 1);

                    // Folded context lines
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

                    const lineAnnotations = line.type === 'deleted' ? [] : (annotationsByLine[line.lineNumber] || []);
                    const hasFlag = lineAnnotations.length > 0;
                    const isRedFlag = lineAnnotations.some(a => a.isRedFlag || a.severity === 'CRITICAL');
                    const isWarning = !isRedFlag && lineAnnotations.some(a => a.severity === 'WARNING');

                    const markerIndex = hasFlag ? flaggedLineNumbers.indexOf(line.lineNumber) : 0;
                    const isAnyCardOpen = lineAnnotations.some((_, cIdx) =>
                      expandedComments[`${currentFile.filename}-${line.lineNumber}-${cIdx}`] ?? true
                    );

                    const rowBgClass = hasFlag
                      ? isRedFlag
                        ? 'suspicious-line-critical'
                        : isWarning
                        ? 'suspicious-line-warning'
                        : 'bg-sky-950/20 hover:bg-sky-950/30'
                      : line.type === 'added'
                      ? 'bg-emerald-950/20'
                      : line.type === 'deleted'
                      ? 'bg-rose-950/20 opacity-70'
                      : 'hover:bg-slate-900/40';

                    const borderLeftClass = hasFlag
                      ? isRedFlag
                        ? 'border-l-4 border-l-rose-500'
                        : isWarning
                        ? 'border-l-4 border-l-amber-500'
                        : 'border-l-4 border-l-sky-500'
                      : '';

                    return (
                      <React.Fragment key={`line-${line.lineNumber}-${idx}`}>
                        <tr
                          id={line.lineNumber > 0 ? `code-line-${line.lineNumber}` : undefined}
                          onClick={() => {
                            if (hasFlag) {
                              lineAnnotations.forEach((_, cIdx) => {
                                const key = `${currentFile.filename}-${line.lineNumber}-${cIdx}`;
                                toggleComment(key);
                              });
                            }
                          }}
                          className={`transition-colors border-b border-slate-900/40 group ${rowBgClass} ${borderLeftClass} ${
                            hasFlag ? 'cursor-pointer' : ''
                          }`}
                        >
                          {/* Gutter 1: Red Flag / Warning Badge */}
                          <td className="w-20 px-2 py-1.5 text-center select-none shrink-0 align-top">
                            {hasFlag ? (
                              <div
                                className="inline-flex items-center justify-center gap-1 animate-stagger-marker"
                                style={{ animationDelay: `${markerIndex * 80}ms` }}
                                title={`Line ${line.lineNumber}: ${lineAnnotations.length} issue(s) detected (Click to expand)`}
                              >
                                {isRedFlag ? (
                                  <div className="flex items-center gap-1 bg-rose-500/20 border border-rose-500/50 px-1.5 py-0.5 rounded-md">
                                    <span className="w-2 h-2 rounded-full bg-rose-500 pulse-dot-critical shadow-[0_0_8px_#f43f5e]" />
                                    <span className="text-[9px] font-black text-rose-300 uppercase tracking-tighter">RED FLAG</span>
                                  </div>
                                ) : isWarning ? (
                                  <div className="flex items-center gap-1 bg-amber-500/20 border border-amber-500/50 px-1.5 py-0.5 rounded-md">
                                    <span className="w-2 h-2 rounded-full bg-amber-400 pulse-dot-warning shadow-[0_0_8px_#f59e0b]" />
                                    <span className="text-[9px] font-black text-amber-300 uppercase tracking-tighter">WARN</span>
                                  </div>
                                ) : (
                                  <span className="w-2.5 h-2.5 rounded-full bg-sky-400 pulse-dot-info shadow-md" />
                                )}
                              </div>
                            ) : (
                              <span className="w-2 h-2 inline-block opacity-0">.</span>
                            )}
                          </td>

                          {/* Gutter 2: Line Number */}
                          <td className={`w-12 px-2 py-1.5 text-right select-none font-mono text-[11px] border-r border-slate-800/80 shrink-0 align-top ${
                            isRedFlag
                              ? 'text-rose-300 font-bold bg-rose-950/40'
                              : isWarning
                              ? 'text-amber-300 font-bold bg-amber-950/40'
                              : 'text-slate-600 group-hover:text-slate-400'
                          }`}>
                            {line.type === 'deleted' ? `−${line.oldLineNumber ?? ''}` : line.lineNumber}
                          </td>

                          {/* Code Content with Red Flag Highlight & Inline Tag */}
                          <td className="px-3 py-1.5 text-slate-200 whitespace-pre overflow-x-auto align-top">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center flex-wrap gap-2">
                                <span
                                  className={`inline-block ${
                                    isRedFlag
                                      ? 'wavy-critical text-rose-100 font-semibold'
                                      : isWarning
                                      ? 'wavy-warning text-amber-100 font-semibold'
                                      : ''
                                  }`}
                                  dangerouslySetInnerHTML={{
                                    __html: highlightSyntax(line.content),
                                  }}
                                />
                                {hasFlag && (
                                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border shadow-sm ${
                                    isRedFlag
                                      ? 'bg-rose-500/20 text-rose-300 border-rose-500/50'
                                      : isWarning
                                      ? 'bg-amber-500/20 text-amber-300 border-amber-500/50'
                                      : 'bg-sky-500/20 text-sky-300 border-sky-500/50'
                                  }`}>
                                    {isRedFlag ? '🚨 SUSPICIOUS' : '⚠️ FLAGGED'}: {lineAnnotations[0]?.ruleId || (isRedFlag ? 'Red Flag' : 'Warning')}
                                  </span>
                                )}
                              </div>
                              {hasFlag && (
                                <span className="text-[10px] text-slate-500 font-sans tracking-wide opacity-0 group-hover:opacity-100 transition-opacity ml-2 shrink-0">
                                  {isAnyCardOpen ? '▼ Hide Rationale' : '▶ Show Rationale & Fix'}
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>

                        {/* In-Flow Detailed Explanation & Fix Card */}
                        {hasFlag &&
                          lineAnnotations.map((ann, cIdx) => {
                            const cardKey = `${currentFile.filename}-${line.lineNumber}-${cIdx}`;
                            const isOpen = expandedComments[cardKey] ?? true;
                            if (!isOpen) return null;

                            const isCrit = ann.isRedFlag || ann.severity === 'CRITICAL';
                            const isWarn = !isCrit && ann.severity === 'WARNING';

                            return (
                              <tr key={`card-${cardKey}`} className="bg-[#080b12] border-b border-slate-800/90">
                                <td colSpan={3} className="p-3 pl-14 pr-4">
                                  <div className={`border rounded-2xl p-4 shadow-2xl space-y-3.5 ${
                                    isCrit
                                      ? 'bg-[#120d14] border-rose-500/40 shadow-rose-950/20'
                                      : isWarn
                                      ? 'bg-[#14120a] border-amber-500/40 shadow-amber-950/20'
                                      : 'bg-[#101422] border-indigo-500/30'
                                  }`}>
                                    {/* Card Header */}
                                    <div className="flex items-center justify-between">
                                      <div className="flex items-center flex-wrap gap-2">
                                        {isCrit ? (
                                          <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
                                        ) : isWarn ? (
                                          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
                                        ) : (
                                          <Info className="w-4 h-4 text-sky-400 shrink-0" />
                                        )}

                                        <span className={`text-[10px] font-black px-2.5 py-0.5 rounded-full border uppercase tracking-wider ${
                                          isCrit
                                            ? 'bg-rose-500/20 text-rose-300 border-rose-500/50'
                                            : isWarn
                                            ? 'bg-amber-500/20 text-amber-300 border-amber-500/50'
                                            : 'bg-sky-500/20 text-sky-300 border-sky-500/50'
                                        }`}>
                                          {ann.badgeTitle}
                                        </span>

                                        {ann.cwe && (
                                          <span className="text-[10px] font-mono bg-slate-800 text-slate-300 px-2 py-0.5 rounded border border-slate-700">
                                            {ann.cwe}
                                          </span>
                                        )}

                                        <span className="text-xs font-semibold text-slate-100">
                                          {ann.commentBody}
                                        </span>
                                      </div>

                                      <button
                                        onClick={e => {
                                          e.stopPropagation();
                                          toggleComment(cardKey);
                                        }}
                                        className="text-slate-400 hover:text-slate-200 text-xs px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-800 hover:border-slate-700 transition-colors flex items-center gap-1 cursor-pointer"
                                      >
                                        <ChevronUp className="w-3.5 h-3.5" />
                                        <span>Hide</span>
                                      </button>
                                    </div>

                                    {/* WHY THIS LINE IS SUSPICIOUS & A RED FLAG */}
                                    <div className={`p-3.5 rounded-xl border flex items-start space-x-3 ${
                                      isCrit
                                        ? 'bg-rose-950/30 border-rose-500/30 text-rose-100'
                                        : isWarn
                                        ? 'bg-amber-950/30 border-amber-500/30 text-amber-100'
                                        : 'bg-indigo-950/30 border-indigo-500/20 text-indigo-100'
                                    }`}>
                                      <span className="text-base select-none shrink-0 mt-0.5">💡</span>
                                      <div className="space-y-1">
                                        <div className={`text-[11px] font-bold uppercase tracking-wider flex items-center gap-2 ${
                                          isCrit ? 'text-rose-400' : isWarn ? 'text-amber-400' : 'text-indigo-300'
                                        }`}>
                                          <span>Why this line is suspicious &amp; a red flag:</span>
                                        </div>
                                        <p className="text-xs leading-relaxed text-slate-200 font-sans">
                                          {ann.whyExplanation}
                                        </p>
                                      </div>
                                    </div>

                                    {/* 1-Click Diff & Suggested Fix */}
                                    {ann.suggestionCode && (
                                      <div className="rounded-xl bg-[#07090e] border border-slate-800/90 overflow-hidden text-xs">
                                        <div className="px-3.5 py-2 bg-[#0d1017] border-b border-slate-800 flex items-center justify-between">
                                          <div className="flex items-center space-x-2">
                                            <Code2 className="w-3.5 h-3.5 text-emerald-400" />
                                            <span className="text-[11px] font-bold text-slate-300 uppercase tracking-wider">
                                              Proposed Secure Patch (1-Click Fix)
                                            </span>
                                          </div>
                                          <button
                                            onClick={e => {
                                              e.stopPropagation();
                                              handleCopyCode(ann.suggestionCode || '', cardKey);
                                            }}
                                            className="text-[11px] text-slate-200 hover:text-white flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 transition-colors cursor-pointer"
                                          >
                                            {copiedKey === cardKey ? (
                                              <>
                                                <Check className="w-3.5 h-3.5 text-emerald-400" />
                                                <span className="text-emerald-300 font-semibold">Copied!</span>
                                              </>
                                            ) : (
                                              <>
                                                <Copy className="w-3.5 h-3.5" />
                                                <span>Copy Secure Fix</span>
                                              </>
                                            )}
                                          </button>
                                        </div>

                                        {/* Old line (Red strikethrough) */}
                                        <div className="px-3.5 py-2 bg-rose-950/25 border-b border-rose-900/30 text-rose-300 font-mono text-[11px] flex items-center space-x-2.5">
                                          <span className="select-none text-rose-500 font-bold text-sm leading-none">-</span>
                                          <span className="line-through opacity-80">{line.content.trim() || '(empty line)'}</span>
                                        </div>

                                        {/* New replacement code (Emerald green) */}
                                        <div className="p-3.5 bg-emerald-950/25 text-emerald-300 font-mono text-[11px] flex items-start space-x-2.5">
                                          <span className="select-none text-emerald-400 font-bold text-sm leading-none shrink-0">+</span>
                                          <pre className="overflow-x-auto leading-relaxed whitespace-pre-wrap flex-1 font-mono">
                                            <code>{ann.suggestionCode.trim()}</code>
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
              const anns = annotationsByLine[lineNo] || [];
              const topSev = anns.some(a => a.isRedFlag || a.severity === 'CRITICAL')
                ? 'CRITICAL'
                : anns.some(a => a.severity === 'WARNING')
                ? 'WARNING'
                : 'INFO';
              const totalLines = Math.max(1, currentFile.lines.length);
              const topPercent = Math.min(96, Math.max(2, (lineNo / totalLines) * 100));
              return (
                <button
                  key={`minimap-${lineNo}`}
                  onClick={() => scrollToLine(lineNo)}
                  title={`Jump to Line ${lineNo} (${topSev}): ${anns[0]?.commentBody || ''}`}
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
