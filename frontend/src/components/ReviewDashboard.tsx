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
import { Tabs } from '../ui/Tabs';

type DashboardTab = 'all' | 'radar' | 'annotated' | 'report' | 'security' | 'governance' | 'inline' | 'impact' | 'tests';

interface ReviewDashboardProps {
  data: ReviewAPIResponse;
  onReset: () => void;
}

export const ReviewDashboard: React.FC<ReviewDashboardProps> = ({ data, onReset }) => {
  const [activeTab, setActiveTab] = useState<DashboardTab>('all');

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
          {data.reviewer_profile_id && <p className="text-xs text-slate-400 break-all font-mono">Reviewer: <code>{data.reviewer_profile_id}</code></p>}
          {(data.file_coverage ?? []).filter(file => !['AVAILABLE', 'DELETED'].includes(file.status)).map(file => (
            <p key={file.path} className="text-amber-300 font-mono text-xs">
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
      <div className="flex flex-wrap items-center justify-between gap-2 bg-surface p-1.5 rounded-xl border border-slate-800/80">
        <Tabs
          label="Review sections"
          idPrefix="review"
          active={activeTab}
          onChange={setActiveTab}
          items={[
            { id: 'all', label: 'Full overview', icon: <LayoutGrid className="w-3.5 h-3.5" /> },
            { id: 'radar', label: 'AI threat radar', icon: <Lock className="w-3.5 h-3.5" /> },
            { id: 'annotated', label: `Annotated code (${totalAnnotatedCount})`, icon: <Sparkles className="w-3.5 h-3.5" /> },
            { id: 'report', label: 'Complete report', icon: <FileText className="w-3.5 h-3.5" /> },
            { id: 'security', label: `Security (${securityCount})`, icon: <ShieldAlert className="w-3.5 h-3.5" /> },
            { id: 'governance', label: `Governance (${govCount})`, icon: <FileCheck className="w-3.5 h-3.5" /> },
            { id: 'inline', label: `Inline fixes (${inlineCount})`, icon: <MessageSquareCode className="w-3.5 h-3.5" /> },
            { id: 'impact', label: 'Call graph', icon: <GitGraph className="w-3.5 h-3.5" /> },
            { id: 'tests', label: 'Generated tests', icon: <TestTube2 className="w-3.5 h-3.5" /> },
          ]}
        />

        <div className="text-xs text-slate-400 font-mono px-2">
          {securityCount} findings • {govCount} violations • {inlineCount} inline suggestions
        </div>
      </div>

      {/* 3. Tab Contents */}
      <div role="tabpanel" id="review-panel" aria-labelledby={`review-tab-${activeTab}`} tabIndex={0} className="focus:outline-none">
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
      </div>

      {/* Telemetry */}
      <TelemetryCard telemetry={data.telemetry} />
    </div>
  );
};
