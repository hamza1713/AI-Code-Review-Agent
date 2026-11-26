import { useEffect, useRef, useState } from 'react';
import { Activity, Search, ShieldAlert } from 'lucide-react';
import {
  getAuditSample,
  getCalibrationMetrics,
  getJson,
  submitAuditDecision,
  submitCalibrationFeedback,
} from '../api';
import { Button, Card, EmptyState, Field, Kpi, Notice, inputClass } from '../ui/primitives';

interface Finding {
  finding_id: string; repo_id: string; pr_id: string; fingerprint: string;
  claim_title: string; claim_description: string; evidence_grade: string;
  generated_test_code: string; outcome: string;
}
interface Metrics {
  total_findings: number; triaged: number; false_positive_rate: number; acceptance_rate?: number;
  audited_total: number; audited_invalid: number; validation_error_rate: number;
}
interface RuleRow {
  rule_id: string; total: number; triaged: number; confirmed_real: number; false_positives: number;
  false_positive_rate: number; acceptance_rate: number; on_probation: boolean;
}
interface Latency { runs: number; latency_p50_seconds: number | null; latency_p95_seconds: number | null }
interface Contracts { outcomes: string[]; audit_reasons: string[] }

const pct = (value: number) => `${(value * 100).toFixed(1)}%`;
const secs = (value: number | null) => (value == null ? '—' : `${value.toFixed(1)}s`);

export function CalibrationView() {
  const [repo, setRepo] = useState('');
  const [loadedRepo, setLoadedRepo] = useState('');
  const [findings, setFindings] = useState<Finding[]>([]);
  const [rules, setRules] = useState<RuleRow[]>([]);
  const [latency, setLatency] = useState<Latency | null>(null);
  const [sampleIds, setSampleIds] = useState<string[]>([]);
  const [selected, setSelected] = useState('');
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [contracts, setContracts] = useState<Contracts | null>(null);
  const [outcome, setOutcome] = useState('FALSE_POSITIVE');
  const [reason, setReason] = useState('OTHER');
  const [notes, setNotes] = useState('');
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { request.current?.abort(); }, []);
  const finding = findings.find(f => f.finding_id === selected);

  const clear = (value: string) => {
    generation.current++;
    request.current?.abort();
    setRepo(value); setLoadedRepo(''); setSelected(''); setFindings([]); setRules([]);
    setMetrics(null); setLatency(null); setNotes(''); setNotice(null); setBusy(false); setSampleIds([]);
  };

  const load = async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const version = ++generation.current;
    const target = repo.trim();
    setBusy(true); setNotice(null);
    try {
      const scope = new URLSearchParams({ repo_id: target });
      const signal = controller.signal;
      const [stats, rows, values, sample, ruleData, latencyData] = await Promise.all([
        getCalibrationMetrics(target, 30, signal),
        getJson(`/api/calibration/findings?${scope}`, signal),
        getJson('/api/calibration/contracts', signal),
        getAuditSample(target, 20, 30, true, signal),
        getJson(`/api/calibration/rules?${scope}`, signal),
        getJson(`/api/telemetry/latency?${scope}`, signal).catch(() => null),
      ]);
      if (generation.current !== version) return;
      setMetrics(stats); setFindings(rows.findings); setContracts(values);
      setRules(ruleData.rules || []); setLatency(latencyData);
      setSampleIds((sample.findings || []).map((f: Finding) => f.finding_id));
      setLoadedRepo(target); setSelected(''); setNotes('');
    } catch (error) {
      if (generation.current === version && !controller.signal.aborted) {
        setMetrics(null); setFindings([]); setRules([]); setSelected(''); setLoadedRepo('');
        setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Calibration data unavailable' });
      }
    } finally { if (generation.current === version) setBusy(false); }
  };

  const submit = async (audit?: boolean) => {
    if (!finding || loadedRepo !== repo.trim()) return;
    const version = generation.current;
    setBusy(true); setNotice(null);
    const scope = { finding_id: finding.finding_id, repo_id: finding.repo_id, pr_id: finding.pr_id };
    try {
      if (audit !== undefined) {
        await submitAuditDecision({ ...scope, is_valid: audit, reason_code: audit ? 'CORRECT_REPRODUCTION' : reason, notes });
      } else await submitCalibrationFeedback({ ...scope, outcome, reason: notes });
      if (generation.current === version) {
        await load();
        setNotice({ tone: 'success', text: 'Decision recorded. Suppression is a separate maintainer action.' });
      }
    } catch (error) {
      if (generation.current === version) setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Decision could not be saved' });
    } finally { if (generation.current === version) setBusy(false); }
  };

  const fprTone = (value: number) => (value > 0.2 ? 'bad' : value > 0.1 ? 'warn' : 'good');
  const ruleRows = rules.filter(r => !r.rule_id.startsWith('category:'));

  return <section className="space-y-6">
    <header className="flex flex-col gap-1">
      <p className="eyebrow">TRUST AND NOISE</p>
      <h1 className="text-2xl font-semibold tracking-tight">Repository calibration</h1>
      <p className="text-sm text-ink-muted">How often developers accept published findings, which rules are noisy, and how long reviews take. Reaction feedback can lag by five minutes or more.</p>
    </header>

    <Card className="p-4">
      <form className="flex flex-col gap-3 sm:flex-row sm:items-end" onSubmit={event => { event.preventDefault(); void load(); }}>
        <div className="flex-1">
          <Field label="Repository"><input aria-label="Repository" className={inputClass}
            value={repo} placeholder="owner/repo" required onChange={event => clear(event.target.value)} /></Field>
        </div>
        <Button type="submit" variant="primary" disabled={busy}><Search size={15} />{busy ? 'Loading…' : 'Load metrics'}</Button>
      </form>
    </Card>

    {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}

    {!metrics && !busy && !notice && (
      <EmptyState title="Pick a repository to see its calibration" icon={<Activity size={28} />}>
        Metrics cover published findings from the last 30 days.
      </EmptyState>
    )}

    {metrics && <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Kpi label="Acceptance rate"
        value={metrics.triaged ? pct(metrics.acceptance_rate ?? 1 - metrics.false_positive_rate) : '—'}
        detail={`${metrics.triaged} triaged of ${metrics.total_findings} published`}
        tone={metrics.triaged ? fprTone(metrics.false_positive_rate) : 'neutral'} />
      <Kpi label="False-positive rate"
        value={metrics.triaged ? pct(metrics.false_positive_rate) : '—'}
        detail="Share of triaged findings marked wrong"
        tone={metrics.triaged ? fprTone(metrics.false_positive_rate) : 'neutral'} />
      <Kpi label="Review latency (p50 / p95)"
        value={latency && latency.runs ? `${secs(latency.latency_p50_seconds)} / ${secs(latency.latency_p95_seconds)}` : '—'}
        detail={latency && latency.runs ? `${latency.runs} reviews recorded` : 'No reviews recorded yet'} />
      <Kpi label="Audit error rate"
        value={metrics.audited_total ? pct(metrics.validation_error_rate) : '—'}
        detail={`${metrics.audited_invalid} invalid of ${metrics.audited_total} audited · ${sampleIds.length} in sample`} />
    </div>}

    {loadedRepo && <Card className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-5 py-3">
        <h2 className="text-sm font-semibold">Rules by false-positive rate</h2>
        <span className="text-xs text-ink-muted">Rules over 10% FP with 10+ triaged go on probation</span>
      </div>
      {ruleRows.length === 0
        ? <p className="px-5 py-6 text-sm text-ink-muted">No triaged findings yet for this repository.</p>
        : <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="bg-raised text-left text-xs text-ink-muted">
            <tr><th className="px-5 py-2.5 font-medium">Rule</th><th className="px-3 py-2.5 font-medium">Triaged</th>
              <th className="px-3 py-2.5 font-medium">Confirmed</th><th className="px-3 py-2.5 font-medium">False positives</th>
              <th className="px-3 py-2.5 font-medium">FP rate</th><th className="px-5 py-2.5 font-medium">Status</th></tr>
          </thead>
          <tbody>{ruleRows.map(rule => <tr key={rule.rule_id} className="border-t border-line">
            <td className="px-5 py-2.5 font-mono text-xs">{rule.rule_id}</td>
            <td className="px-3 py-2.5">{rule.triaged}</td>
            <td className="px-3 py-2.5">{rule.confirmed_real}</td>
            <td className="px-3 py-2.5">{rule.false_positives}</td>
            <td className="px-3 py-2.5">{rule.triaged ? pct(rule.false_positive_rate) : '—'}</td>
            <td className="px-5 py-2.5">{rule.on_probation
              ? <span className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[11px] font-semibold text-amber-200"><ShieldAlert size={12} />Probation</span>
              : <span className="text-xs text-ink-muted">Active</span>}</td>
          </tr>)}</tbody>
        </table></div>}
    </Card>}

    {loadedRepo && !busy && findings.length === 0 && <p className="text-sm text-ink-muted">No published findings in the selected period.</p>}

    {!!findings.length && <Card className="space-y-4 p-5">
      <h2 className="text-sm font-semibold">Record feedback on a finding</h2>
      <Field label="Finding and PR">
        <select className={inputClass} value={selected}
          onChange={event => { setSelected(event.target.value); setNotes(''); setNotice(null); }}>
          <option value="">Select a finding</option>
          {findings.map(f => <option key={f.finding_id} value={f.finding_id}>
            PR #{f.pr_id} — {f.claim_title || f.fingerprint}{sampleIds.includes(f.finding_id) ? ' (audit sample)' : ''}
          </option>)}
        </select>
      </Field>
      {finding && <div className="space-y-4 rounded-xl border border-line bg-canvas p-4">
        <div>
          <p className="text-sm font-semibold">{finding.repo_id} · PR #{finding.pr_id}</p>
          <p className="mt-1 text-sm text-ink-muted">{finding.claim_description}</p>
          <p className="mt-2 text-xs text-ink-muted">{finding.evidence_grade} · {finding.outcome} · <span className="break-all font-mono">{finding.finding_id}</span></p>
        </div>
        <Field label="Notes"><textarea className={inputClass} value={notes} onChange={e => setNotes(e.target.value)} /></Field>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1"><Field label="Feedback">
            <select className={inputClass} value={outcome} onChange={e => setOutcome(e.target.value)}>
              {contracts?.outcomes.map(value => <option key={value}>{value}</option>)}
            </select>
          </Field></div>
          <Button variant="primary" disabled={busy} onClick={() => void submit()}>Save feedback</Button>
        </div>
        {finding.evidence_grade === 'REPRODUCED' && <div className="space-y-3 border-t border-line pt-4">
          <h3 className="text-sm font-semibold">Review the recorded reproduction</h3>
          <pre className="overflow-auto text-xs">{finding.generated_test_code}</pre>
          <Field label="Rejection reason">
            <select className={inputClass} value={reason} onChange={e => setReason(e.target.value)}>
              {contracts?.audit_reasons.filter(v => v !== 'CORRECT_REPRODUCTION').map(value => <option key={value}>{value}</option>)}
            </select>
          </Field>
          <div className="flex flex-wrap gap-3">
            <Button variant="success" disabled={busy} onClick={() => void submit(true)}>Confirm reproduction</Button>
            <Button variant="danger" disabled={busy} onClick={() => void submit(false)}>Reject reproduction</Button>
          </div>
        </div>}
      </div>}
    </Card>}
  </section>;
}
