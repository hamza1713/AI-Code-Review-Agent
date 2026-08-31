import React, { useState } from 'react';
import { ShieldAlert, FileCheck, MessageSquareCode, GitGraph, TestTube2, LayoutGrid, FileText } from 'lucide-react';
import type { ReviewAPIResponse } from '../types/review';
import { VerdictBanner } from './VerdictBanner';
import { FindingsList } from './FindingsList';
import { GovernanceViolations } from './GovernanceViolations';
import { InlineCommentsList } from './InlineCommentsList';
import { CrossFileImpact } from './CrossFileImpact';
import { GeneratedUnitTests } from './GeneratedUnitTests';
import { TelemetryCard } from './TelemetryCard';
import { ScopeDisclaimer } from './ScopeDisclaimer';
import { CompleteExecutiveReport } from './CompleteExecutiveReport';
import { TraceVisualizer } from './TraceVisualizer';
import { AnnotatedCodeViewer } from './AnnotatedCodeViewer';
import { Sparkles } from 'lucide-react';


interface ReviewDashboardProps {
  data: ReviewAPIResponse;
  onReset: () => void;
}

export const ReviewDashboard: React.FC<ReviewDashboardProps> = ({ data, onReset }) => {
  const [activeTab, setActiveTab] = useState<'all' | 'annotated' | 'report' | 'security' | 'governance' | 'inline' | 'impact' | 'tests'>('all');


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

  const securityCount = data.pattern_findings?.length || 0;
  const govCount = data.governance_violations?.length || 0;
  const inlineCount = data.inline_comments?.length || 0;

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

      {/* 2. Navigation Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-2 bg-[#12151c] p-1.5 rounded-xl border border-slate-800/80">
        <div className="flex flex-wrap items-center gap-1">
          <button
            onClick={() => setActiveTab('all')}
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
            onClick={() => setActiveTab('annotated')}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'annotated'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Sparkles className="w-3.5 h-3.5 text-amber-400" />
            <span>Annotated Code ({inlineCount})</span>
          </button>

          <button
            onClick={() => setActiveTab('report')}
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
            onClick={() => setActiveTab('security')}
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
            onClick={() => setActiveTab('governance')}
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
            onClick={() => setActiveTab('inline')}
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
            onClick={() => setActiveTab('impact')}
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
            onClick={() => setActiveTab('tests')}
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
          {/* Animated Interactive Code Annotation View */}
          <AnnotatedCodeViewer
            reviewedDiff={data.reviewed_diff}
            inlineComments={data.inline_comments || []}
          />

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
            {/* Left Column: Security & Governance */}
            <div className="space-y-4">
              <FindingsList
                findings={data.pattern_findings || []}
                label={data.pattern_findings_label}
              />
              <GovernanceViolations
                violations={data.governance_violations || []}
              />
            </div>

            {/* Right Column: Tests, Inline Suggestions & Call Graph */}
            <div className="space-y-4">
              <GeneratedUnitTests unitTests={data.generated_unit_tests} />
              <InlineCommentsList comments={data.inline_comments || []} />
              <CrossFileImpact impact={data.cross_file_impact} />
            </div>
          </div>
        </div>
      )}

      {activeTab === 'annotated' && (
        <AnnotatedCodeViewer
          reviewedDiff={data.reviewed_diff}
          inlineComments={data.inline_comments || []}
        />
      )}

      {activeTab === 'report' && (
        <CompleteExecutiveReport
          data={data}
        />
      )}

      {activeTab === 'security' && (
        <FindingsList
          findings={data.pattern_findings || []}
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
          />
          <InlineCommentsList comments={data.inline_comments || []} />
        </div>
      )}



      {activeTab === 'impact' && (
        <CrossFileImpact impact={data.cross_file_impact} />
      )}

      {activeTab === 'tests' && (
        <GeneratedUnitTests unitTests={data.generated_unit_tests} />
      )}

      {/* 4. Trace Visualizer */}
      <TraceVisualizer trace={data.trace} />

      {/* 5. Telemetry */}
      <TelemetryCard telemetry={data.telemetry} />

      {/* 6. Scope Disclaimer */}
      <ScopeDisclaimer note={data.scope_note} />
    </div>
  );
};


