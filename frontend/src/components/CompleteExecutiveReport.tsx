import React, { useState, useMemo } from 'react';
import { FileText, Copy, Check, Download } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ReviewAPIResponse } from '../types/review';

interface CompleteExecutiveReportProps {
  data: ReviewAPIResponse;
}

/**
 * Builds the full structured markdown report from all available data fields,
 * matching exactly the format printed in the terminal. Used as a fallback
 * when the backend does not populate `full_report`.
 */
function buildFullReport(data: ReviewAPIResponse): string {
  const { verdict, confidence_score, summary, pattern_findings = [], governance_violations = [] } = data;

  const lines: string[] = [];

  // ── 1. Final Decision ────────────────────────────────────────────────────
  lines.push('# Pull Request Review Report', '');
  lines.push('## 1. Final Decision');
  lines.push(`**${verdict}**`, '');

  // ── 2. Confidence Score ──────────────────────────────────────────────────
  lines.push('## 2. Confidence Score');
  lines.push(`**${confidence_score}**`, '');

  // ── 3. Executive Summary ─────────────────────────────────────────────────
  lines.push('## 3. Executive Summary');
  lines.push(summary, '');

  // ── 4. Detailed Findings ─────────────────────────────────────────────────
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

  // ── 5. Required Action Items ─────────────────────────────────────────────
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

  // Cross-file impact
  if (data.cross_file_impact?.available) {
    lines.push('*   **Cross-File Impact**: ');
    lines.push(`    *   ${data.cross_file_impact.message}`);
    lines.push('');
  }

  // Testing gate
  if (data.generated_unit_tests) {
    lines.push('*   **Testing & CI**:');
    lines.push('    *   Implement the provided unit test suite to verify the authentication logic and simulate edge cases.');
    lines.push('    *   Integrate static analysis tools (e.g., `bandit`) into the CI pipeline to automatically detect and block future occurrences of these vulnerabilities.');
    lines.push('');
  }

  // Approval gate
  const hasBlocking = pattern_findings.some(f => f.severity === 'CRITICAL' || f.severity === 'HIGH')
    || governance_violations.some(v => v.severity === 'BLOCKING');

  if (hasBlocking) {
    lines.push('*   **Approval Gate**: Do not merge until all critical security vulnerabilities are addressed and the unit tests pass successfully in a clean environment.');
    lines.push('');
  }

  // ── 6. Scope Note ────────────────────────────────────────────────────────
  if (data.scope_note) {
    lines.push('---', '');
    lines.push('## 6. Scope Note', '');
    lines.push(`*${data.scope_note}*`, '');
  }

  return lines.join('\n');
}

export const CompleteExecutiveReport: React.FC<CompleteExecutiveReportProps> = ({ data }) => {
  const [copied, setCopied] = useState(false);

  // Use backend's full_report if available; otherwise synthesise it from structured data
  const displayReport = useMemo(() => {
    const raw = data.full_report;
    if (raw && raw.trim()) return raw.trim();
    return buildFullReport(data);
  }, [data]);

  const handleCopy = () => {
    navigator.clipboard.writeText(displayReport);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDownloadMd = () => {
    const blob = new Blob([displayReport], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `PR_Review_Report_${data.verdict}_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-5 shadow-lg space-y-4">
      {/* Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800/80">
        <div className="flex items-center space-x-2.5">
          <div className="w-8 h-8 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <FileText className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-bold text-white tracking-tight uppercase">
                Complete Executive PR Review Report
              </h3>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-md bg-indigo-950 text-indigo-300 border border-indigo-800/50">
                Confidence: {data.confidence_score}/100
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Synthesized multi-agent evaluation, risk matrix, and required remediation roadmap
            </p>
          </div>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleCopy}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-slate-700 transition-all cursor-pointer"
            title="Copy full Markdown report to clipboard"
          >
            {copied ? (
              <>
                <Check className="w-3.5 h-3.5 text-emerald-400" />
                <span>Copied!</span>
              </>
            ) : (
              <>
                <Copy className="w-3.5 h-3.5" />
                <span>Copy Markdown</span>
              </>
            )}
          </button>

          <button
            onClick={handleDownloadMd}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-semibold border border-indigo-500/30 transition-all cursor-pointer"
            title="Download report as .md file"
          >
            <Download className="w-3.5 h-3.5" />
            <span>Download .md</span>
          </button>
        </div>
      </div>

      {/* Rendered Markdown Body */}
      <div className="bg-[#0a0c10] border border-slate-800/80 rounded-xl p-5 text-slate-200 text-xs leading-relaxed max-h-[600px] overflow-y-auto pr-3 prose prose-invert max-w-none">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {displayReport}
        </ReactMarkdown>
      </div>
    </div>
  );
};
