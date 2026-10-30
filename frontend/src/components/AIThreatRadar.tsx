import React, { useState } from 'react';
import {
  Fingerprint,
  Download,
  CheckCircle2,
  Lock,
  Boxes,
  FileCode2,
  Binary,
  Scale,
} from 'lucide-react';
import type { ReviewAPIResponse } from '../types/review';

interface AIThreatRadarProps {
  data: ReviewAPIResponse;
}

export const AIThreatRadar: React.FC<AIThreatRadarProps> = ({ data }) => {
  const [downloadingSarif, setDownloadingSarif] = useState(false);

  // Extract findings relevant to AI-specific threats
  const allFindings = [
    ...(data.reconciled_findings || []),
    ...(data.findings || []),
    ...(data.pattern_findings || []),
  ];

  // Supply Chain & Slopsquatting
  const slopFindings = allFindings.filter(f =>
    f.rule_id === 'SEC-SLOPSQUAT-001' || f.rule_id === 'SEC-TYPOSQUAT-002' || f.rule_id === 'SEC-MALICIOUS-PKG-001'
  );
  const isSupplyChainClean = slopFindings.length === 0;

  // Trojan Source & Prompt Injection
  const trojanFindings = allFindings.filter(f =>
    f.rule_id === 'SEC-TROJAN-001' || f.rule_id === 'SEC-PI-001' || f.rule_id?.includes('BIDI')
  );
  const isTrojanClean = trojanFindings.length === 0;

  // AI-Generated Code Defects
  const aiDefectFindings = allFindings.filter(f =>
    f.rule_id === 'AI-ASYNC-001' ||
    f.rule_id === 'AI-ASYNC-002' ||
    f.rule_id === 'AI-STUB-001' ||
    f.rule_id === 'AI-CONTRACT-001'
  );
  const isAIDefectsClean = aiDefectFindings.length === 0;

  // Cross-Model Consensus
  const consensusAudit = data.ai_threat_audit?.consensus || null;

  const handleDownloadSARIF = async () => {
    setDownloadingSarif(true);
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('review_api_token') : null;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }
      const response = await fetch('/api/review/sarif', {
        method: 'POST',
        headers,
        body: JSON.stringify(data),
      });

      let sarifBlob: Blob;
      if (response.ok) {
        const sarifData = await response.json();
        sarifBlob = new Blob([JSON.stringify(sarifData, null, 2)], { type: 'application/sarif+json' });
      } else {
        // Client-side fallback SARIF 2.1.0 generation
        const fallbackSarif = {
          $schema: 'https://json.schemastore.org/sarif-2.1.0.json',
          version: '2.1.0',
          runs: [
            {
              tool: {
                driver: {
                  name: 'AICodeReviewAgent',
                  semanticVersion: '3.0.0',
                  rules: allFindings.map(f => ({
                    id: f.rule_id,
                    shortDescription: { text: f.description },
                    defaultConfiguration: { level: f.severity === 'CRITICAL' ? 'error' : 'warning' },
                  })),
                },
              },
              results: allFindings.map(f => ({
                ruleId: f.rule_id,
                level: f.severity === 'CRITICAL' ? 'error' : 'warning',
                message: { text: `${f.description}\nFix: ${f.fix_recommendation}` },
                locations: [
                  {
                    physicalLocation: {
                      artifactLocation: { uri: f.file_path },
                      region: { startLine: f.line_number || 1 },
                    },
                  },
                ],
              })),
            },
          ],
        };
        sarifBlob = new Blob([JSON.stringify(fallbackSarif, null, 2)], { type: 'application/sarif+json' });
      }

      const url = URL.createObjectURL(sarifBlob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `review-attestation-${new Date().toISOString().slice(0, 10)}.sarif`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Failed to export SARIF:', err);
    } finally {
      setDownloadingSarif(false);
    }
  };

  const snapshotHash = data.input_snapshot_hash || null;

  return (
    <div className="bg-[#10141e] border border-slate-800 rounded-2xl p-5 shadow-xl space-y-5">
      {/* Top Banner */}
      <div className="flex flex-wrap items-center justify-between pb-4 border-b border-slate-800 gap-3">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 rounded-xl bg-indigo-500/10 border border-indigo-500/30 flex items-center justify-center text-indigo-400">
            <Lock className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-white uppercase tracking-wider">
                AI-Era Zero-Trust Threat Radar
              </h2>
              <span className="text-[10px] font-mono bg-indigo-950/80 text-indigo-300 border border-indigo-800 px-2 py-0.5 rounded-full font-bold">
                Pillar v3.0 Defense
              </span>
            </div>
            <p className="text-xs text-slate-400">
              Deterministic verification against AI hallucinations, supply chain slopsquatting, and Trojan Source
            </p>
          </div>
        </div>

        {/* Action: Export SARIF 2.1.0 */}
        <button
          onClick={handleDownloadSARIF}
          disabled={downloadingSarif}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition-colors cursor-pointer shadow-sm disabled:opacity-50"
        >
          <Download className="w-3.5 h-3.5" />
          <span>{downloadingSarif ? 'Exporting...' : 'Export SARIF 2.1.0'}</span>
        </button>
      </div>

      {/* 4 Defense Radar Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Card 1: Supply Chain & Slopsquatting */}
        <div className={`p-4 rounded-xl border flex flex-col justify-between ${
          isSupplyChainClean
            ? 'bg-emerald-950/15 border-emerald-500/30'
            : 'bg-rose-950/20 border-rose-500/40'
        }`}>
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center space-x-2">
                <Boxes className={`w-4 h-4 ${isSupplyChainClean ? 'text-emerald-400' : 'text-rose-400'}`} />
                <span className="text-xs font-bold text-white">Supply Chain</span>
              </div>
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border ${
                isSupplyChainClean
                  ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
                  : 'bg-rose-500/20 text-rose-300 border-rose-500/40'
              }`}>
                {isSupplyChainClean ? 'CLEAN' : `${slopFindings.length} THREAT(S)`}
              </span>
            </div>
            <p className="text-[11px] text-slate-300 leading-relaxed">
              Manifest & import verification against hallucinated packages (slopsquatting) and Levenshtein typosquatting.
            </p>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800/80 text-[10px] font-mono text-slate-400">
            {isSupplyChainClean ? (
              <span className="text-emerald-400 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> 0 Hallucinated Packages
              </span>
            ) : (
              <span className="text-rose-400 font-bold">
                🚨 {slopFindings[0]?.rule_id}: {slopFindings[0]?.description.slice(0, 35)}...
              </span>
            )}
          </div>
        </div>

        {/* Card 2: Trojan Source & Canary Sandboxing */}
        <div className={`p-4 rounded-xl border flex flex-col justify-between ${
          isTrojanClean
            ? 'bg-emerald-950/15 border-emerald-500/30'
            : 'bg-rose-950/20 border-rose-500/40'
        }`}>
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center space-x-2">
                <Binary className={`w-4 h-4 ${isTrojanClean ? 'text-emerald-400' : 'text-rose-400'}`} />
                <span className="text-xs font-bold text-white">Trojan Source</span>
              </div>
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border ${
                isTrojanClean
                  ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
                  : 'bg-rose-500/20 text-rose-300 border-rose-500/40'
              }`}>
                {isTrojanClean ? 'SHIELDED' : 'OVERRIDE DETECTED'}
              </span>
            </div>
            <p className="text-[11px] text-slate-300 leading-relaxed">
              Unicode Bidi overrides (CVE-2021-42574), invisible zero-width chars, and canary prompt token sandboxing.
            </p>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800/80 text-[10px] font-mono text-slate-400">
            {isTrojanClean ? (
              <span className="text-emerald-400 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> Canary Token Validated
              </span>
            ) : (
              <span className="text-rose-400 font-bold">
                🚨 {trojanFindings[0]?.rule_id}: Bidi/Unicode injection
              </span>
            )}
          </div>
        </div>

        {/* Card 3: AI Code Defect Detectors */}
        <div className={`p-4 rounded-xl border flex flex-col justify-between ${
          isAIDefectsClean
            ? 'bg-emerald-950/15 border-emerald-500/30'
            : 'bg-amber-950/20 border-amber-500/40'
        }`}>
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center space-x-2">
                <FileCode2 className={`w-4 h-4 ${isAIDefectsClean ? 'text-emerald-400' : 'text-amber-400'}`} />
                <span className="text-xs font-bold text-white">AI Code Smells</span>
              </div>
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border ${
                isAIDefectsClean
                  ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
                  : 'bg-amber-500/20 text-amber-300 border-amber-500/40'
              }`}>
                {isAIDefectsClean ? '0 DEFECTS' : `${aiDefectFindings.length} FLAGGED`}
              </span>
            </div>
            <p className="text-[11px] text-slate-300 leading-relaxed">
              Catches unawaited async coroutines in conditionals, blocking I/O in async def, and hollow mock stubs.
            </p>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800/80 text-[10px] font-mono text-slate-400">
            {isAIDefectsClean ? (
              <span className="text-emerald-400 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> Concurrency & Contract Integrity
              </span>
            ) : (
              <span className="text-amber-400 font-bold">
                ⚠️ {aiDefectFindings[0]?.rule_id}: {aiDefectFindings[0]?.description.slice(0, 32)}...
              </span>
            )}
          </div>
        </div>

        {/* Card 4: Model Engine & Consensus */}
        <div className="p-4 rounded-xl border border-indigo-500/30 bg-indigo-950/20 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center space-x-2">
                <Scale className="w-4 h-4 text-indigo-400" />
                <span className="text-xs font-bold text-white">Model Consensus</span>
              </div>
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border bg-indigo-500/20 text-indigo-300 border-indigo-500/40">
                {consensusAudit ? `${consensusAudit.agreement_score}% MATCH` : 'SINGLE ENGINE'}
              </span>
            </div>
            <p className="text-[11px] text-slate-300 leading-relaxed">
              {consensusAudit
                ? 'Orthogonal cross-family verification pairing primary reviewer with an independent red-team critic.'
                : 'Primary analyzer executed. Multi-model red-team critic consensus is available in enterprise configuration.'}
            </p>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800/80 text-[10px] font-mono text-indigo-300 flex items-center justify-between">
            <span>Engine: {consensusAudit ? `Critic: ${consensusAudit.critic_verdict}` : (data.telemetry?.model_used || 'Gemini')}</span>
            <span className="text-emerald-400">✓ {consensusAudit ? 'INDEPENDENT' : 'PRIMARY'}</span>
          </div>
        </div>
      </div>

      {/* Cryptographic Attestation Receipt Footer */}
      <div className="p-3 bg-[#0a0d14] border border-slate-800/80 rounded-xl flex flex-wrap items-center justify-between gap-2 text-xs">
        <div className="flex items-center space-x-2 text-slate-300">
          <Fingerprint className="w-4 h-4 text-indigo-400" />
          <span className="font-semibold text-slate-200">Cryptographic Review Receipt:</span>
          {snapshotHash ? (
            <code className="text-indigo-300 font-mono text-[11px] bg-slate-900 px-2 py-0.5 rounded border border-slate-800">
              sha256:{snapshotHash.slice(0, 16)}...
            </code>
          ) : (
            <span className="text-slate-400 font-mono text-[11px] italic bg-slate-900 px-2 py-0.5 rounded border border-slate-800">
              Unsigned (Development Run)
            </span>
          )}
        </div>
        <div className="text-[11px] font-mono text-slate-400">
          OASIS SARIF 2.1.0 • GitHub Code Scanning Ready • Full Worktree Isolation
        </div>
      </div>
    </div>
  );
};
