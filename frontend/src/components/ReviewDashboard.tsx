import React, { useState } from 'react';
import { ShieldAlert, FileCheck, MessageSquareCode, GitGraph, TestTube2, LayoutGrid, FileText, Sparkles, Lock } from 'lucide-react';
import type { ReviewAPIResponse } from '../types/review';
import { VerdictBanner } from './VerdictBanner';
import { FindingsList } from './FindingsList';
import { GovernanceViolations } from './GovernanceViolations';
import { InlineCommentsList } from './InlineCommentsList';
import { CrossFileImpact } from './CrossFileImpact';
import { GeneratedUnitTests } from './GeneratedUnitTests';
import { TelemetryCard } from './TelemetryCard';
import { CompleteExecutiveReport } from './CompleteExecutiveReport';
import { AnnotatedCodeViewer } from './AnnotatedCodeViewer';
import { AIThreatRadar } from './AIThreatRadar';

interface ReviewDashboardProps {
  data: ReviewAPIResponse;
  onReset: () => void;
}

export const ReviewDashboard: React.FC<ReviewDashboardProps> = ({ data, onReset }) => {
  const [activeTab, setActiveTab] = useState<'all' | 'radar' | 'annotated' | 'report' | 'security' | 'governance' | 'inline' | 'impact' | 'tests'>('all');

  const handleExportJSON = () => {
    const jsonString = `data:text/json;charset=utf-8,${encodeURIComponent(
      JSON.stringify(data, null, 2)
    )}`;
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute('href', jsonString);
    downloadAnchor.setAttribute('download', `code_review_report_${new Date().toISOString().slice(0, 10)}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
  };

  const securityCount = (data.findings ?? data.pattern_findings)?.length || 0;
  const govCount = data.governance_violations?.length || 0;
  const inlineCount = data.inline_comments?.length || 0;
  const totalAnnotatedCount = securityCount + govCount + inlineCount;

  const hasCoverageIssues = data.analysis_complete === false || (data.file_coverage ?? []).some(f => !['AVAILABLE', 'DELETED'].includes(f.status));

  return (
    <div className="space-y-4">
      {/* 1. Verdict Banner */}
      <VerdictBanner
        verdict={data.verdict}
        confidenceScore={data.confidence_score}
        summary={data.summary}
        onReset={onReset}
        onExportJSON={handleExportJSON}
      />

      {/* Coverage Status (rendered when coverage issues exist) */}
      {hasCoverageIssues && (
        <section aria-label="Review coverage" className="rounded-xl border border-amber-500/30 bg-amber-950/10 p-3 text-xs text-slate-300 space-y-1.5">
          <div className="flex items-center gap-2 text-amber-300 font-semibold">
            <span>Incomplete analysis — manual review required</span>
            {data.reviewer_profile?.model && <span className="text-slate-400 font-normal">({data.reviewer_profile.model})</span>}
          </div>
          {data.reviewer_profile_id && <p className="text-[11px] text-slate-400 break-all font-mono">Reviewer: <code>{data.reviewer_profile_id}</code></p>}
          {(data.file_coverage ?? []).filter(file => !['AVAILABLE', 'DELETED'].includes(file.status)).map(file => (
            <p key={file.path} className="text-amber-300 font-mono text-[11px]">
              <code>{file.path}</code>: {file.status.toLowerCase().replaceAll('_', ' ')}{file.reason ? ` — ${file.reason}` : ''}
            </p>
          ))}
        </section>
      )}

      {/* Review Metrics */}
      <section className="review-metrics" aria-label="Review counts">
        <button className="review-metric" aria-pressed={activeTab === 'security'} onClick={() => setActiveTab('security')}>
          <span>Security findings</span>
          <strong className="text-rose-300">{securityCount}</strong>
          <small>Explore risks and fixes →</small>
        </button>
        <button className="review-metric" aria-pressed={activeTab === 'governance'} onClick={() => setActiveTab('governance')}>
          <span>Standards violations</span>
          <strong className="text-amber-300">{govCount}</strong>
          <small>Review team rules →</small>
        </button>
        <button className="review-metric" aria-pressed={activeTab === 'inline'} onClick={() => setActiveTab('inline')}>
          <span>Inline suggestions</span>
          <strong className="text-indigo-300">{inlineCount}</strong>
          <small>See proposed changes →</small>
        </button>
      </section>

      {/* 2. Navigation Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-2 bg-[#12151c] p-1.5 rounded-xl border border-slate-800/80">
        <div className="flex flex-wrap items-center gap-1">
          <button
            aria-pressed={activeTab === 'all'} onClick={() => setActiveTab('all')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'all'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <LayoutGrid className="w-3.5 h-3.5" />
            <span>Full Overview</span>
          </button>

          <button
            aria-pressed={activeTab === 'radar'} onClick={() => setActiveTab('radar')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'radar'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Lock className="w-3.5 h-3.5 text-indigo-400" />
            <span>AI Threat Radar</span>
          </button>

          <button
            aria-pressed={activeTab === 'annotated'} onClick={() => setActiveTab('annotated')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'annotated'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Sparkles className="w-3.5 h-3.5 text-amber-400" />
            <span>Annotated Code ({totalAnnotatedCount})</span>
          </button>

          <button
            aria-pressed={activeTab === 'report'} onClick={() => setActiveTab('report')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'report'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <FileText className="w-3.5 h-3.5 text-indigo-400" />
            <span>Complete Report</span>
          </button>

          <button
            aria-pressed={activeTab === 'security'} onClick={() => setActiveTab('security')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'security'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <ShieldAlert className="w-3.5 h-3.5 text-rose-400" />
            <span>Security ({securityCount})</span>
          </button>

          <button
            aria-pressed={activeTab === 'governance'} onClick={() => setActiveTab('governance')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'governance'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <FileCheck className="w-3.5 h-3.5 text-amber-400" />
            <span>Governance ({govCount})</span>
          </button>

          <button
            aria-pressed={activeTab === 'inline'} onClick={() => setActiveTab('inline')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'inline'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <MessageSquareCode className="w-3.5 h-3.5 text-indigo-400" />
            <span>Inline Fixes ({inlineCount})</span>
          </button>

          <button
            aria-pressed={activeTab === 'impact'} onClick={() => setActiveTab('impact')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'impact'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <GitGraph className="w-3.5 h-3.5 text-cyan-400" />
            <span>AST Call Graph</span>
          </button>

          <button
            aria-pressed={activeTab === 'tests'} onClick={() => setActiveTab('tests')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'tests'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <TestTube2 className="w-3.5 h-3.5 text-emerald-400" />
            <span>Generated Pytest</span>
          </button>
        </div>

        <div className="text-[10px] text-slate-400 font-mono px-2">
          {securityCount} findings • {govCount} violations • {inlineCount} inline suggestions
        </div>
      </div>

      {/* 3. Tab Contents */}
      {activeTab === 'all' && (
        <div className="space-y-4">
          {/* AI-Era Zero-Trust Threat Radar */}
          <AIThreatRadar data={data} />

          {/* Animated Interactive Code Annotation View */}
          <AnnotatedCodeViewer
            reviewedDiff={data.reviewed_diff}
            inlineComments={data.inline_comments || []}
            findings={data.findings ?? data.pattern_findings ?? []}
            governanceViolations={data.governance_violations || []}
          />

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
            {/* Left Column: Security & Governance */}
            <div className="space-y-4">
              <FindingsList
                findings={data.findings ?? data.pattern_findings ?? []}
                label={data.pattern_findings_label}
              />
              <GovernanceViolations
                violations={data.governance_violations || []}
              />
            </div>

            {/* Right Column: Tests, Inline Suggestions & Call Graph */}
            <div className="space-y-4">
              <GeneratedUnitTests unitTests={data.generated_unit_tests} execution={data.test_execution} />
              <InlineCommentsList comments={data.inline_comments || []} />
              <CrossFileImpact impact={data.cross_file_impact} />
            </div>
          </div>
        </div>
      )}

      {activeTab === 'radar' && (
        <AIThreatRadar data={data} />
      )}

      {activeTab === 'annotated' && (
        <AnnotatedCodeViewer
          reviewedDiff={data.reviewed_diff}
          inlineComments={data.inline_comments || []}
          findings={data.findings ?? data.pattern_findings ?? []}
          governanceViolations={data.governance_violations || []}
        />
      )}

      {activeTab === 'report' && (
        <CompleteExecutiveReport
          data={data}
        />
      )}

      {activeTab === 'security' && (
        <FindingsList
          findings={data.findings ?? data.pattern_findings ?? []}
          label={data.pattern_findings_label}
        />
      )}

      {activeTab === 'governance' && (
        <GovernanceViolations
          violations={data.governance_violations || []}
        />
      )}

      {activeTab === 'inline' && (
        <div className="space-y-4">
          <AnnotatedCodeViewer
            reviewedDiff={data.reviewed_diff}
            inlineComments={data.inline_comments || []}
            findings={data.findings ?? data.pattern_findings ?? []}
            governanceViolations={data.governance_violations || []}
          />
          <InlineCommentsList comments={data.inline_comments || []} />
        </div>
      )}

      {activeTab === 'impact' && (
        <CrossFileImpact impact={data.cross_file_impact} />
      )}

      {activeTab === 'tests' && (
        <GeneratedUnitTests unitTests={data.generated_unit_tests} execution={data.test_execution} />
      )}

      {/* Telemetry */}
      <TelemetryCard telemetry={data.telemetry} />
    </div>
  );
};
