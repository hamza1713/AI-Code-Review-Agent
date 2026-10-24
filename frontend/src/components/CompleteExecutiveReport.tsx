import React, { useState, useMemo } from 'react';
import {
  FileText,
  Copy,
  Check,
  Download,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  OctagonAlert,
  Search,
  ChevronDown,
  ChevronRight,
  Info,
  Printer,
  Wrench,
  Sparkles,
  X,
  Code2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ReviewAPIResponse, SastFinding } from '../types/review';

interface CompleteExecutiveReportProps {
  data: ReviewAPIResponse;
}

interface ParsedFinding {
  id: string;
  index: number;
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO';
  cwe: string;
  location: string;
  filePath: string;
  lineNumber: number;
  defect: string;
  sources: string[];
  fixRecommendation?: string;
  snippet?: string;
  isBlocking: boolean;
}

/**
 * Builds the full structured markdown report fallback when the backend
 * does not populate `full_report`.
 */
function buildFullReport(data: ReviewAPIResponse): string {
  const { verdict, confidence_score, summary, governance_violations = [] } = data;
  const pattern_findings = (data.pattern_findings && data.pattern_findings.length > 0)
    ? data.pattern_findings
    : (data.findings ?? []);
  const lines: string[] = [];

  lines.push('# Pull Request Review Report', '');
  lines.push('## 1. Final Decision');
  lines.push(`**${verdict}**`, '');

  lines.push('## 2. Confidence Score');
  lines.push(`**${confidence_score}**`, '');

  lines.push('## 3. Executive Summary');
  lines.push(summary, '');

  lines.push('## 4. Detailed Findings', '');
  if (pattern_findings.length > 0 || governance_violations.length > 0) {
    lines.push('| File | Line | Severity | Issue |');
    lines.push('| :--- | :--- | :--- | :--- |');

    for (const f of pattern_findings) {
      const sev = f.severity === 'CRITICAL' ? '**CRITICAL**' : `**${f.severity}**`;
      lines.push(`| \`${f.file_path}\` | ${f.line_number} | ${sev} | ${f.description} |`);
    }

    for (const v of governance_violations) {
      const sev = v.severity === 'BLOCKING' ? '**BLOCKING**' : `**${v.severity}**`;
      lines.push(`| \`${v.file_path}\` | ${v.line_number} | ${sev} | **${v.rule_name}**: ${v.description} |`);
    }
    lines.push('');
  } else {
    lines.push('*No security findings or governance violations detected.*', '');
  }

  lines.push('## 5. Required Action Items', '');
  if (pattern_findings.length > 0) {
    lines.push('*   **Immediate Refactor**: ');
    const uniqueFixes = [...new Set(pattern_findings.map(f => f.fix_recommendation).filter(Boolean))];
    for (const fix of uniqueFixes) {
      lines.push(`    *   ${fix}`);
    }
    lines.push('');
  }

  if (governance_violations.length > 0) {
    lines.push('*   **Governance Remediation**: ');
    const uniqueGov = [...new Set(governance_violations.map(v => v.suggested_fix).filter(Boolean))];
    for (const fix of uniqueGov) {
      lines.push(`    *   ${fix}`);
    }
    lines.push('');
  }

  if (data.cross_file_impact?.available) {
    lines.push('*   **Cross-File Impact**: ');
    lines.push(`    *   ${data.cross_file_impact.message}`);
    lines.push('');
  }

  if (data.scope_note) {
    lines.push('---', '');
    lines.push('## 6. Scope Note', '');
    lines.push(`*${data.scope_note}*`, '');
  }

  return lines.join('\n');
}

/**
 * Extracts structured reconciled findings from the markdown report table
 * and joins them with pre-scan metadata for snippets and recommendations.
 */
function extractStructuredFindings(
  reportText: string,
  rawSast: SastFinding[],
  blockingList: string[]
): ParsedFinding[] {
  const findings: ParsedFinding[] = [];
  const lines = reportText.split('\n');
  let inTable = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('| # |') || trimmed.includes('| Severity | CWE |')) {
      inTable = true;
      continue;
    }
    if (inTable) {
      if (!trimmed.startsWith('|') || trimmed.startsWith('| :--') || trimmed.startsWith('|:--')) {
        if (!trimmed.startsWith('|')) {
          inTable = false;
        }
        continue;
      }

      // Format: | 1 | 🔴 CRITICAL | CWE-89 | `file.py:L55` | Defect description | ast, bandit |
      const parts = trimmed
        .split('|')
        .map(p => p.trim())
        .filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);

      if (parts.length >= 5) {
        const rawIdx = parseInt(parts[0], 10) || findings.length + 1;
        const rawSev = parts[1].replace(/[🔴🟠🟡🔵⚪?]/gu, '').trim().toUpperCase();
        const severity = (['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'].includes(rawSev)
          ? rawSev
          : 'HIGH') as ParsedFinding['severity'];

        const cwe = parts[2].trim() || 'CWE-Unknown';
        const rawLoc = parts[3].replace(/[`]/g, '').trim();
        const defect = parts[4].replace(/\\\|/g, '|').trim();
        const rawSources = parts[5] ? parts[5].split(',').map(s => s.trim()).filter(Boolean) : [];

        let filePath = rawLoc;
        let lineNumber = 0;
        const locMatch = rawLoc.match(/^(.*?):L?(\d+)$/i);
        if (locMatch) {
          filePath = locMatch[1];
          lineNumber = parseInt(locMatch[2], 10);
        }

        // Match with pre-scan SAST findings for recommendations and snippets
        const match = rawSast.find(
          s =>
            (filePath && s.file_path && s.file_path.includes(filePath)) ||
            (lineNumber > 0 && Math.abs(s.line_number - lineNumber) <= 3) ||
            (s.cwe && cwe && s.cwe.toLowerCase() === cwe.toLowerCase())
        );

        const isBlocking =
          severity === 'CRITICAL' ||
          blockingList.some(
            b =>
              b.toLowerCase().includes(filePath.toLowerCase()) ||
              (cwe !== 'CWE-Unknown' && b.toLowerCase().includes(cwe.toLowerCase())) ||
              (lineNumber > 0 && b.includes(`L${lineNumber}`))
          );

        findings.push({
          id: `finding-${rawIdx}-${filePath}-${lineNumber}`,
          index: rawIdx,
          severity,
          cwe,
          location: rawLoc,
          filePath,
          lineNumber,
          defect,
          sources: rawSources,
          fixRecommendation: match?.fix_recommendation,
          snippet: match?.snippet,
          isBlocking,
        });
      }
    }
  }

  // Fallback: If report didn't contain a table, construct findings directly from raw SAST
  if (findings.length === 0 && rawSast.length > 0) {
    return rawSast.map((s, idx) => ({
      id: `sast-${idx}-${s.file_path}-${s.line_number}`,
      index: idx + 1,
      severity: (s.severity || 'HIGH') as ParsedFinding['severity'],
      cwe: s.cwe || 'CWE-Unknown',
      location: `${s.file_path}:L${s.line_number}`,
      filePath: s.file_path,
      lineNumber: s.line_number,
      defect: s.description,
      sources: ['sast-scanner'],
      fixRecommendation: s.fix_recommendation,
      snippet: s.snippet,
      isBlocking: s.severity === 'CRITICAL',
    }));
  }

  return findings;
}

export const CompleteExecutiveReport: React.FC<CompleteExecutiveReportProps> = ({ data }) => {
  const [viewMode, setViewMode] = useState<'dashboard' | 'document' | 'raw'>('dashboard');
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedSeverity, setSelectedSeverity] = useState<string>('ALL');
  const [onlyBlocking, setOnlyBlocking] = useState(false);
  const [showScoreBreakdown, setShowScoreBreakdown] = useState(false);
  const [expandedFindings, setExpandedFindings] = useState<Record<string, boolean>>({});
  const [copiedFixId, setCopiedFixId] = useState<string | null>(null);
  const [copiedReport, setCopiedReport] = useState(false);

  // Full report text
  const displayReport = useMemo(() => {
    const raw = data.full_report;
    if (raw && raw.trim()) return raw.trim();
    return buildFullReport(data);
  }, [data]);

  // Extract Blocking Reasons
  const blockingReasons = useMemo(() => {
    const match = displayReport.match(/###\s*(?:🚫\s*)?Blocking[\r\n]+([\s\S]*?)(?=(?:\||##|---|$))/i);
    if (match) {
      return match[1]
        .split('\n')
        .map(l => l.replace(/^[-*]\s*/, '').trim())
        .filter(Boolean);
    }
    // Fallback: collect critical findings and blocking governance rules
    const list: string[] = [];
    const resolvedFindings = (data.pattern_findings && data.pattern_findings.length > 0)
      ? data.pattern_findings
      : (data.findings || []);
    resolvedFindings
      .filter(f => f.severity === 'CRITICAL')
      .forEach(f => list.push(`CRITICAL ${f.cwe || ''} in ${f.file_path}:L${f.line_number} — ${f.description}`));
    (data.governance_violations || [])
      .filter(v => v.severity === 'BLOCKING')
      .forEach(v => list.push(`BLOCKING rule ${v.rule_name} in ${v.file_path}:L${v.line_number}`));
    return list;
  }, [displayReport, data]);

  // Extract Quality Score and calculation breakdown
  const { qualityScore, scoreBreakdown } = useMemo(() => {
    const scoreMatch =
      displayReport.match(/\*\*Quality Score\*\*:\s*(\d+)\/100/i) ||
      displayReport.match(/Quality Score:\s*(\d+)\/100/i);
    const score = scoreMatch ? parseInt(scoreMatch[1], 10) : data.confidence_score;

    const breakdownMatch = displayReport.match(/\*Score\*:\s*`([^`]+)`/i);
    const breakdown = breakdownMatch ? breakdownMatch[1] : '';

    return { qualityScore: score, scoreBreakdown: breakdown };
  }, [displayReport, data.confidence_score]);

  // Extract Test Evidence
  const testEvidence = useMemo(() => {
    const badge =
      data.test_execution?.evidence_badge ||
      (displayReport.includes('🔴 VULNERABILITY CONFIRMED')
        ? '🔴 VULNERABILITY CONFIRMED'
        : displayReport.includes('🟢 FIX VERIFIED')
        ? '🟢 FIX VERIFIED'
        : '⚪ HEURISTIC');

    const summary =
      data.test_execution?.summary_message ||
      (badge.includes('VULNERABILITY CONFIRMED')
        ? 'A generated test reproduced the defect on PR code — confirms the vulnerability is present in current branch.'
        : badge.includes('FIX VERIFIED')
        ? 'A post-fix regression test passed — proposed fix behaves safely as intended.'
        : 'Based on static analysis signals without dynamic test execution.');

    return { badge, summary };
  }, [data.test_execution, displayReport]);

  // Structured Reconciled Findings
  const allFindings = useMemo(() => {
    if (data.reconciled_findings && data.reconciled_findings.length > 0) {
      return data.reconciled_findings.map((s, idx): ParsedFinding => ({
        id: `reconciled-${idx}-${s.file_path}-${s.line_number}`,
        index: idx + 1,
        severity: (s.severity || 'HIGH') as ParsedFinding['severity'],
        cwe: s.cwe || 'CWE-Unknown',
        location: `${s.file_path}:L${s.line_number}`,
        filePath: s.file_path,
        lineNumber: s.line_number,
        defect: s.description,
        sources: s.analyzer_source ? String(s.analyzer_source).split(',') : ['reconciled'],
        fixRecommendation: s.fix_recommendation,
        snippet: s.snippet,
        isBlocking: s.blocking || s.severity === 'CRITICAL',
      }));
    }
    const rawFindings = (data.pattern_findings && data.pattern_findings.length > 0)
      ? data.pattern_findings
      : (data.findings || []);
    return extractStructuredFindings(displayReport, rawFindings, blockingReasons);
  }, [displayReport, data.reconciled_findings, data.pattern_findings, data.findings, blockingReasons]);

  // Finding counts by severity
  const severityCounts = useMemo(() => {
    const counts = { ALL: allFindings.length, CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 };
    for (const f of allFindings) {
      if (f.severity in counts) {
        counts[f.severity as keyof typeof counts]++;
      }
    }
    return counts;
  }, [allFindings]);

  // Filtered Findings
  const filteredFindings = useMemo(() => {
    return allFindings.filter(f => {
      if (selectedSeverity !== 'ALL' && f.severity !== selectedSeverity) return false;
      if (onlyBlocking && !f.isBlocking) return false;
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const matchText = `${f.defect} ${f.cwe} ${f.filePath} ${f.location} ${f.sources.join(' ')}`.toLowerCase();
        if (!matchText.includes(q)) return false;
      }
      return true;
    });
  }, [allFindings, selectedSeverity, onlyBlocking, searchQuery]);

  // Toggle card expansion
  const toggleExpand = (id: string) => {
    setExpandedFindings(prev => ({ ...prev, [id]: !prev[id] }));
  };

  // Copy single fix
  const handleCopyFix = async (id: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedFixId(id);
      setTimeout(() => setCopiedFixId(null), 2000);
    } catch {
      // fallback
    }
  };

  // Copy full markdown
  const handleCopyReport = async () => {
    try {
      await navigator.clipboard.writeText(displayReport);
      setCopiedReport(true);
      setTimeout(() => setCopiedReport(false), 2000);
    } catch {
      // fallback
    }
  };

  // Download markdown
  const handleDownloadMd = () => {
    const blob = new Blob([displayReport], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `PR_Executive_Report_${data.verdict}_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  // Verdict style mapper
  const verdictStyle = useMemo(() => {
    const v = (data.verdict || '').toUpperCase();
    if (v.includes('ESCALATE')) {
      return {
        badgeBg: 'bg-rose-500/10 border-rose-500/30 text-rose-400',
        dotColor: 'bg-rose-500',
        icon: <OctagonAlert className="w-5 h-5 text-rose-400" />,
        label: 'ESCALATE — MERGE BLOCKED',
        desc: 'Critical vulnerabilities or blocking governance policies detected. Immediate refactoring required.',
      };
    }
    if (v.includes('REQUEST')) {
      return {
        badgeBg: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
        dotColor: 'bg-amber-500',
        icon: <AlertTriangle className="w-5 h-5 text-amber-400" />,
        label: 'REQUEST CHANGES',
        desc: 'Remediation required for identified security flaws or anti-patterns before approval.',
      };
    }
    return {
      badgeBg: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
      dotColor: 'bg-emerald-500',
      icon: <CheckCircle2 className="w-5 h-5 text-emerald-400" />,
      label: 'APPROVED',
      desc: 'No blocking flaws detected. Code meets enterprise quality and security standards.',
    };
  }, [data.verdict]);

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-5 shadow-2xl space-y-6">
      {/* ── TOP HEADER BAR ────────────────────────────────────────── */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-slate-800/80">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400 shrink-0">
            <FileText className="w-5 h-5" />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-bold text-white tracking-tight uppercase">
                Executive PR Review Report
              </h2>
              <span className={`text-[11px] font-bold px-2.5 py-0.5 rounded-full border ${verdictStyle.badgeBg} flex items-center gap-1.5`}>
                <span className={`w-1.5 h-1.5 rounded-full ${verdictStyle.dotColor} animate-pulse`} />
                {verdictStyle.label}
              </span>
              <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-indigo-950/80 text-indigo-300 border border-indigo-800/50">
                Confidence: {data.confidence_score}/100
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Synthesized multi-agent evaluation, deterministic risk scoring, and interactive remediation roadmap
            </p>
          </div>
        </div>

        {/* View Mode Switcher & Export Controls */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Mode Switcher */}
          <div className="flex items-center bg-[#0a0c10] p-1 rounded-lg border border-slate-800">
            <button
              onClick={() => setViewMode('dashboard')}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
                viewMode === 'dashboard'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>Dashboard</span>
            </button>
            <button
              onClick={() => setViewMode('document')}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
                viewMode === 'document'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <FileText className="w-3.5 h-3.5" />
              <span>Formatted Doc</span>
            </button>
            <button
              onClick={() => setViewMode('raw')}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
                viewMode === 'raw'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Code2 className="w-3.5 h-3.5" />
              <span>Raw .md</span>
            </button>
          </div>

          {/* Action Buttons */}
          <button
            onClick={handleCopyReport}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-slate-700 transition-all cursor-pointer"
            title="Copy full Markdown report to clipboard"
          >
            {copiedReport ? (
              <>
                <Check className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-emerald-400">Copied!</span>
              </>
            ) : (
              <>
                <Copy className="w-3.5 h-3.5" />
                <span>Copy</span>
              </>
            )}
          </button>

          <button
            onClick={handleDownloadMd}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-semibold border border-indigo-500/30 transition-all cursor-pointer"
            title="Download report as .md file"
          >
            <Download className="w-3.5 h-3.5" />
            <span>Download</span>
          </button>

          <button
            onClick={() => window.print()}
            className="hidden sm:flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-400 hover:text-slate-200 text-xs font-semibold border border-slate-800 transition-all cursor-pointer"
            title="Print or export as PDF"
          >
            <Printer className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* ── KPI METRIC CARDS (4 CARDS GRID) ───────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
        {/* Card 1: Verdict */}
        <div className={`p-4 rounded-xl border ${verdictStyle.badgeBg} flex flex-col justify-between`}>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-slate-300 uppercase tracking-wider">Merge Decision</span>
            {verdictStyle.icon}
          </div>
          <div className="mt-2">
            <div className="text-lg font-extrabold text-white tracking-tight">{data.verdict}</div>
            <p className="text-[11px] text-slate-300 mt-1 line-clamp-2">{verdictStyle.desc}</p>
          </div>
        </div>

        {/* Card 2: Quality Score */}
        <div className="p-4 rounded-xl border border-slate-800 bg-[#0a0c10] flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Quality Score</span>
            <span className="text-xs font-mono font-bold text-indigo-400">{qualityScore}/100</span>
          </div>
          <div className="mt-2">
            <div className="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
              <div
                className={`h-full transition-all duration-500 ${
                  qualityScore >= 80
                    ? 'bg-emerald-500'
                    : qualityScore >= 50
                    ? 'bg-amber-500'
                    : 'bg-rose-500'
                }`}
                style={{ width: `${qualityScore}%` }}
              />
            </div>
            {scoreBreakdown && (
              <button
                onClick={() => setShowScoreBreakdown(!showScoreBreakdown)}
                className="mt-2 flex items-center gap-1 text-[10px] text-indigo-400 hover:text-indigo-300 transition-colors cursor-pointer"
              >
                <Info className="w-3 h-3" />
                <span>{showScoreBreakdown ? 'Hide calculation' : 'View formula breakdown'}</span>
              </button>
            )}
          </div>
        </div>

        {/* Card 3: Test Evidence */}
        <div className="p-4 rounded-xl border border-slate-800 bg-[#0a0c10] flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Test Evidence</span>
            <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-slate-800 text-slate-200 border border-slate-700">
              {testEvidence.badge}
            </span>
          </div>
          <div className="mt-2">
            <p className="text-[11px] text-slate-300 leading-tight line-clamp-3">
              {testEvidence.summary}
            </p>
          </div>
        </div>

        {/* Card 4: Findings Summary */}
        <div className="p-4 rounded-xl border border-slate-800 bg-[#0a0c10] flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Total Findings</span>
            <span className="text-sm font-extrabold text-white font-mono">{allFindings.length}</span>
          </div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {severityCounts.CRITICAL > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/30">
                {severityCounts.CRITICAL} Critical
              </span>
            )}
            {severityCounts.HIGH > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-orange-500/10 text-orange-400 border border-orange-500/30">
                {severityCounts.HIGH} High
              </span>
            )}
            {severityCounts.MEDIUM > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">
                {severityCounts.MEDIUM} Med
              </span>
            )}
            {severityCounts.LOW > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-500/10 text-blue-400 border border-blue-500/30">
                {severityCounts.LOW} Low
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Expanded Score Calculation Drawer */}
      {showScoreBreakdown && scoreBreakdown && (
        <div className="p-3.5 rounded-xl bg-indigo-950/20 border border-indigo-800/40 text-xs text-indigo-200 font-mono space-y-1">
          <div className="font-semibold text-indigo-300 flex items-center gap-1.5">
            <Info className="w-3.5 h-3.5" />
            <span>Deterministic Scoring Formula:</span>
          </div>
          <p className="text-indigo-200/90 leading-relaxed text-[11px]">{scoreBreakdown}</p>
        </div>
      )}

      {/* ── VIEW MODE: DASHBOARD ──────────────────────────────────── */}
      {viewMode === 'dashboard' && (
        <div className="space-y-5">
          {/* Blocking Violations Callout Banner */}
          {blockingReasons.length > 0 && (
            <div className="p-4 rounded-xl bg-rose-950/20 border border-rose-600/40 space-y-2.5">
              <div className="flex items-center gap-2 text-rose-400 font-bold text-xs uppercase tracking-wide">
                <OctagonAlert className="w-4 h-4 shrink-0" />
                <span>Blocking Violations (Must Resolve Before Merge)</span>
                <span className="px-2 py-0.5 rounded-full bg-rose-950 text-rose-300 text-[10px] border border-rose-800">
                  {blockingReasons.length}
                </span>
              </div>
              <ul className="space-y-1.5 text-xs text-rose-200/90 pl-5 list-disc">
                {blockingReasons.map((reason, idx) => (
                  <li key={idx} className="leading-relaxed">
                    {reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Search & Severity Filter Bar */}
          <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 bg-[#0a0c10] p-2.5 rounded-xl border border-slate-800">
            {/* Search Input */}
            <div className="relative flex-1">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                placeholder="Search findings by CWE, file path, rule, or defect text..."
                className="w-full pl-9 pr-8 py-1.5 rounded-lg bg-slate-900 border border-slate-800 text-xs text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>

            {/* Severity Filter Pills */}
            <div className="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0">
              {(['ALL', 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const).map(sev => {
                const count = severityCounts[sev];
                const active = selectedSeverity === sev;
                return (
                  <button
                    key={sev}
                    onClick={() => setSelectedSeverity(sev)}
                    className={`px-2.5 py-1 rounded-lg text-xs font-semibold transition-all whitespace-nowrap cursor-pointer flex items-center gap-1.5 ${
                      active
                        ? 'bg-indigo-600 text-white shadow-sm'
                        : 'bg-slate-900 text-slate-400 hover:text-slate-200 border border-slate-800 hover:border-slate-700'
                    }`}
                  >
                    <span>{sev === 'ALL' ? 'All' : sev.charAt(0) + sev.slice(1).toLowerCase()}</span>
                    <span className="text-[10px] px-1.5 py-0.2 rounded bg-black/30 font-mono">
                      {count}
                    </span>
                  </button>
                );
              })}

              {/* Only Blocking Toggle */}
              {blockingReasons.length > 0 && (
                <button
                  onClick={() => setOnlyBlocking(!onlyBlocking)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-semibold transition-all whitespace-nowrap cursor-pointer border ${
                    onlyBlocking
                      ? 'bg-rose-950 text-rose-300 border-rose-700'
                      : 'bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200'
                  }`}
                >
                  Blocking Only
                </button>
              )}
            </div>
          </div>

          {/* Interactive Reconciled Findings Cards */}
          <div className="space-y-3">
            <div className="flex items-center justify-between text-xs text-slate-400 px-1">
              <span>
                Showing {filteredFindings.length} of {allFindings.length} defect(s)
              </span>
              {filteredFindings.length > 0 && (
                <button
                  onClick={() => {
                    const allExpanded = filteredFindings.every(f => expandedFindings[f.id]);
                    const next: Record<string, boolean> = {};
                    filteredFindings.forEach(f => {
                      next[f.id] = !allExpanded;
                    });
                    setExpandedFindings(next);
                  }}
                  className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors cursor-pointer"
                >
                  {filteredFindings.every(f => expandedFindings[f.id]) ? 'Collapse All' : 'Expand All'}
                </button>
              )}
            </div>

            {filteredFindings.length === 0 ? (
              <div className="text-center py-12 rounded-xl bg-[#0a0c10] border border-slate-800 text-slate-400 space-y-2">
                <CheckCircle2 className="w-8 h-8 mx-auto text-emerald-400" />
                <p className="text-sm font-semibold text-slate-200">No findings match your filter criteria.</p>
                <p className="text-xs">Try selecting 'All' or clearing your search term.</p>
              </div>
            ) : (
              filteredFindings.map(f => {
                const isExpanded = !!expandedFindings[f.id];
                const sevColor =
                  f.severity === 'CRITICAL'
                    ? 'border-l-rose-500 bg-rose-500/5'
                    : f.severity === 'HIGH'
                    ? 'border-l-orange-500 bg-orange-500/5'
                    : f.severity === 'MEDIUM'
                    ? 'border-l-amber-500 bg-amber-500/5'
                    : 'border-l-blue-500 bg-blue-500/5';

                const sevBadge =
                  f.severity === 'CRITICAL'
                    ? 'bg-rose-950 text-rose-300 border-rose-800/80'
                    : f.severity === 'HIGH'
                    ? 'bg-orange-950 text-orange-300 border-orange-800/80'
                    : f.severity === 'MEDIUM'
                    ? 'bg-amber-950 text-amber-300 border-amber-800/80'
                    : 'bg-blue-950 text-blue-300 border-blue-800/80';

                return (
                  <div
                    key={f.id}
                    className={`rounded-xl border border-slate-800 bg-[#0a0c10] border-l-4 ${sevColor} p-4 transition-all hover:border-slate-700 space-y-3`}
                  >
                    {/* Header Row */}
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-400 font-bold">#{f.index}</span>
                        <span className={`text-[10px] font-extrabold px-2 py-0.5 rounded border ${sevBadge}`}>
                          {f.severity}
                        </span>
                        {f.cwe && (
                          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-900 text-indigo-300 border border-indigo-900/60 font-semibold">
                            {f.cwe}
                          </span>
                        )}
                        <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-900 text-slate-300 border border-slate-800">
                          {f.location}
                        </span>
                        {f.isBlocking && (
                          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-rose-950/80 text-rose-300 border border-rose-800 flex items-center gap-1">
                            <OctagonAlert className="w-3 h-3" />
                            Blocking
                          </span>
                        )}
                      </div>

                      {/* Source Chips & Expand toggle */}
                      <div className="flex items-center gap-2">
                        <div className="flex items-center gap-1">
                          {f.sources.map(src => (
                            <span
                              key={src}
                              className="text-[9px] uppercase px-1.5 py-0.5 rounded bg-slate-800/80 text-slate-400 font-mono"
                            >
                              {src}
                            </span>
                          ))}
                        </div>

                        <button
                          onClick={() => toggleExpand(f.id)}
                          className="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition-colors cursor-pointer"
                          title={isExpanded ? 'Collapse' : 'Expand'}
                        >
                          {isExpanded ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                        </button>
                      </div>
                    </div>

                    {/* Defect Description */}
                    <div className="text-xs text-slate-200 font-medium leading-relaxed">
                      {f.defect}
                    </div>

                    {/* Expandable Snippet & Fix Section */}
                    {isExpanded && (
                      <div className="space-y-3 pt-2 border-t border-slate-800/80">
                        {/* Snippet preview if available */}
                        {f.snippet && (
                          <div className="space-y-1">
                            <div className="text-[10px] font-mono uppercase text-slate-400 flex items-center gap-1">
                              <Code2 className="w-3 h-3" />
                              <span>Code Context:</span>
                            </div>
                            <pre className="p-2.5 rounded-lg bg-[#07090d] border border-slate-800 text-[11px] text-slate-300 font-mono overflow-x-auto">
                              <code>{f.snippet}</code>
                            </pre>
                          </div>
                        )}

                        {/* Remediation Recommendation with 1-click Copy */}
                        {f.fixRecommendation && (
                          <div className="p-3 rounded-lg bg-indigo-950/20 border border-indigo-800/40 space-y-2">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-1.5 text-xs font-semibold text-indigo-300">
                                <Wrench className="w-3.5 h-3.5 text-indigo-400" />
                                <span>Remediation Recommendation</span>
                              </div>
                              <button
                                onClick={() => handleCopyFix(f.id, f.fixRecommendation!)}
                                className="flex items-center gap-1 px-2 py-0.5 rounded bg-indigo-900/40 hover:bg-indigo-900/70 text-indigo-300 text-[10px] font-semibold border border-indigo-700/50 transition-colors cursor-pointer"
                              >
                                {copiedFixId === f.id ? (
                                  <>
                                    <Check className="w-3 h-3 text-emerald-400" />
                                    <span className="text-emerald-400">Copied!</span>
                                  </>
                                ) : (
                                  <>
                                    <Copy className="w-3 h-3" />
                                    <span>Copy Fix</span>
                                  </>
                                )}
                              </button>
                            </div>
                            <p className="text-[11px] text-slate-300 leading-relaxed font-mono">
                              {f.fixRecommendation}
                            </p>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Remediation Roadmap Section */}
          <div className="p-4 rounded-xl bg-[#0a0c10] border border-slate-800 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Wrench className="w-4 h-4 text-indigo-400" />
                <h3 className="text-xs font-bold text-white uppercase tracking-wider">
                  Remediation Roadmap & Next Steps
                </h3>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <div className="p-3 rounded-lg bg-slate-900/60 border border-slate-800/80 space-y-1.5">
                <span className="text-[11px] font-bold text-rose-300 flex items-center gap-1.5">
                  <ShieldAlert className="w-3.5 h-3.5" />
                  <span>Immediate Security Refactoring</span>
                </span>
                <p className="text-[11px] text-slate-300 leading-relaxed">
                  Address all SQL injection (`CWE-89`), OS command execution (`CWE-78`), and eval/exec (`CWE-95`) vulnerabilities. Replace raw string concatenations with parameterized statements and safe execution APIs.
                </p>
              </div>

              <div className="p-3 rounded-lg bg-slate-900/60 border border-slate-800/80 space-y-1.5">
                <span className="text-[11px] font-bold text-amber-300 flex items-center gap-1.5">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>Governance & CI Gate Verification</span>
                </span>
                <p className="text-[11px] text-slate-300 leading-relaxed">
                  Ensure password hashes use secure PBKDF2/Argon2 algorithms (`CWE-916`) rather than legacy MD5/SHA1. Execute the generated pytest test suite to confirm all defect assertions pass.
                </p>
              </div>
            </div>
          </div>

          {/* Scope & Limitations Disclosure */}
          <div className="p-3.5 rounded-xl bg-slate-900/40 border border-slate-800/60 text-[11px] text-slate-400 leading-relaxed flex items-start gap-2.5">
            <Info className="w-4 h-4 text-slate-500 shrink-0 mt-0.5" />
            <div>
              <span className="font-semibold text-slate-300">Coverage & Audit Scope: </span>
              {data.scope_note ||
                'This synthesized report combines Bandit/SAST pattern detection, AST cross-file call graphs, and LLM expert evaluation. Treat this analysis as an automated pre-merge gating layer alongside manual team peer review.'}
            </div>
          </div>
        </div>
      )}

      {/* ── VIEW MODE: FORMATTED DOCUMENT ─────────────────────────── */}
      {viewMode === 'document' && (
        <div className="bg-[#0a0c10] border border-slate-800/80 rounded-xl p-6 text-slate-200 text-xs leading-relaxed max-h-[750px] overflow-y-auto">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              table: ({ children }) => (
                <div className="overflow-x-auto my-4 rounded-xl border border-slate-800 bg-[#0d1117] shadow-inner">
                  <table className="w-full text-left text-xs border-collapse">{children}</table>
                </div>
              ),
              thead: ({ children }) => (
                <thead className="bg-slate-900/90 text-slate-300 font-semibold border-b border-slate-700">
                  {children}
                </thead>
              ),
              th: ({ children }) => (
                <th className="px-4 py-3 text-slate-300 font-semibold text-xs tracking-wider uppercase border-r border-slate-800 last:border-r-0 whitespace-nowrap">
                  {children}
                </th>
              ),
              td: ({ children }) => (
                <td className="px-4 py-3 border-t border-slate-800/80 text-slate-200 text-xs border-r border-slate-800/50 last:border-r-0 hover:bg-slate-800/30 transition-colors">
                  {children}
                </td>
              ),
              h1: ({ children }) => (
                <h1 className="text-lg font-bold text-white pb-2 mb-4 border-b border-slate-800 flex items-center gap-2">
                  <FileText className="w-5 h-5 text-indigo-400" />
                  <span>{children}</span>
                </h1>
              ),
              h2: ({ children }) => (
                <h2 className="text-sm font-bold text-indigo-300 mt-6 mb-3 pb-1.5 border-b border-slate-800/60 flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-indigo-400" />
                  <span>{children}</span>
                </h2>
              ),
              h3: ({ children }) => (
                <h3 className="text-xs font-semibold text-slate-200 mt-4 mb-2 flex items-center gap-1.5">
                  <ChevronRight className="w-3.5 h-3.5 text-slate-400" />
                  <span>{children}</span>
                </h3>
              ),
              code: ({ children, className }) => {
                const isBlock = className?.includes('language-');
                if (isBlock) {
                  return (
                    <code className="block p-3 rounded-lg bg-[#07090d] border border-slate-800 font-mono text-[11px] text-indigo-200 overflow-x-auto my-2">
                      {children}
                    </code>
                  );
                }
                return (
                  <code className="px-1.5 py-0.5 rounded bg-indigo-950/70 border border-indigo-800/40 text-indigo-300 font-mono text-[11px]">
                    {children}
                  </code>
                );
              },
              blockquote: ({ children }) => (
                <blockquote className="p-3 my-3 rounded-lg bg-slate-900/60 border-l-4 border-indigo-500 text-slate-300 text-xs italic">
                  {children}
                </blockquote>
              ),
              ul: ({ children }) => (
                <ul className="list-disc pl-5 my-2 space-y-1.5 text-slate-300 text-xs">{children}</ul>
              ),
              ol: ({ children }) => (
                <ol className="list-decimal pl-5 my-2 space-y-1.5 text-slate-300 text-xs">{children}</ol>
              ),
            }}
          >
            {displayReport}
          </ReactMarkdown>
        </div>
      )}

      {/* ── VIEW MODE: RAW MARKDOWN ───────────────────────────────── */}
      {viewMode === 'raw' && (
        <div className="bg-[#0a0c10] border border-slate-800/80 rounded-xl p-4 space-y-3">
          <div className="flex items-center justify-between text-xs text-slate-400 pb-2 border-b border-slate-800">
            <span className="font-mono text-[11px]">Markdown Source ({displayReport.split('\n').length} lines)</span>
            <button
              onClick={handleCopyReport}
              className="flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer"
            >
              {copiedReport ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              <span>{copiedReport ? 'Copied' : 'Copy markdown'}</span>
            </button>
          </div>
          <pre className="font-mono text-xs text-slate-300 max-h-[600px] overflow-y-auto whitespace-pre-wrap leading-relaxed select-all">
            {displayReport}
          </pre>
        </div>
      )}
    </div>
  );
};
