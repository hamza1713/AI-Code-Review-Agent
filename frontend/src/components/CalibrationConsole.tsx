import { useEffect, useRef, useState } from 'react';
import { apiFetch, responseError, submitAuditDecision, submitCalibrationFeedback } from '../api';

interface Finding {
  finding_id: string; repo_id: string; pr_id: string; fingerprint: string;
  claim_title: string; claim_description: string; evidence_grade: string;
  generated_test_code: string; outcome: string;
}
interface Metrics {
  total_findings: number; triaged: number; false_positive_rate: number;
  audited_total: number; audited_invalid: number; validation_error_rate: number;
}
interface Contracts { outcomes: string[]; audit_reasons: string[] }

export function CalibrationView() {
  const [repo, setRepo] = useState('');
  const [loadedRepo, setLoadedRepo] = useState('');
  const [findings, setFindings] = useState<Finding[]>([]);
  const [sampleIds, setSampleIds] = useState<string[]>([]);
  const [selected, setSelected] = useState('');
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [contracts, setContracts] = useState<Contracts | null>(null);
  const [outcome, setOutcome] = useState('FALSE_POSITIVE');
  const [reason, setReason] = useState('OTHER');
  const [notes, setNotes] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { request.current?.abort(); }, []);
  const finding = findings.find(f => f.finding_id === selected);
  const inputClass = 'rounded border border-slate-600 bg-slate-950 p-3 w-full';

  const clear = (value: string) => {
    generation.current++;
    request.current?.abort();
    setRepo(value); setLoadedRepo(''); setSelected(''); setFindings([]);
    setMetrics(null); setNotes(''); setMessage(''); setBusy(false); setSampleIds([]);
  };
  const load = async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const version = ++generation.current;
    const target = repo.trim();
    setBusy(true); setMessage('');
    try {
      const read = async (path: string) => {
        const response = await apiFetch(path, { signal: controller.signal });
        if (!response.ok) throw new Error(await responseError(response));
        return response.json();
      };
      const scope = new URLSearchParams({ repo_id: target });
      const [stats, rows, values, sample] = await Promise.all([
        read(`/api/calibration/metrics?${scope}`), read(`/api/calibration/findings?${scope}`),
        read('/api/calibration/contracts'), read(`/api/calibration/audit/sample?${scope}`),
      ]);
      if (generation.current !== version) return;
      setMetrics(stats); setFindings(rows.findings); setContracts(values);
      setSampleIds((sample.findings || []).map((f: Finding) => f.finding_id));
      setLoadedRepo(target); setSelected(''); setNotes('');
    } catch (error) {
      if (generation.current === version && !controller.signal.aborted) {
        setMetrics(null); setFindings([]); setSelected(''); setLoadedRepo('');
        setMessage(error instanceof Error ? error.message : 'Calibration data unavailable');
      }
    } finally { if (generation.current === version) setBusy(false); }
  };
  const submit = async (audit?: boolean) => {
    if (!finding || loadedRepo !== repo.trim()) return;
    const version = generation.current;
    setBusy(true); setMessage('');
    const scope = { finding_id: finding.finding_id, repo_id: finding.repo_id, pr_id: finding.pr_id };
    try {
      if (audit !== undefined) {
        await submitAuditDecision({ ...scope, is_valid: audit,
          reason_code: audit ? 'CORRECT_REPRODUCTION' : reason, notes });
      } else await submitCalibrationFeedback({ ...scope, outcome, reason: notes });
      if (generation.current === version) { await load(); setMessage('Decision recorded. Suppression is a separate maintainer action.'); }
    } catch (error) {
      if (generation.current === version) setMessage(error instanceof Error ? error.message : 'Decision could not be saved');
    } finally { if (generation.current === version) setBusy(false); }
  };
  return <section className="space-y-5">
    <h1 className="text-2xl font-semibold">Repository calibration</h1>
    <p>Published findings only. Reaction feedback may take five minutes or longer under provider rate limits.</p>
    <form className="flex gap-3" onSubmit={event => { event.preventDefault(); void load(); }}>
      <label className="flex-1">Repository<input aria-label="Repository" className={inputClass}
        value={repo} placeholder="owner/repo" required onChange={event => clear(event.target.value)} /></label>
      <button disabled={busy} className="rounded bg-indigo-600 px-5">Load</button>
    </form>
    {message && <p role="status">{message}</p>}
    {busy && <p>Loading…</p>}
    {metrics && <div className="grid gap-4 sm:grid-cols-3">
      <div className="rounded border p-4"><h2>False-positive rate</h2>
        <strong>{metrics.triaged ? `${(metrics.false_positive_rate*100).toFixed(1)}%` : 'Not enough data'}</strong>
        <p>{metrics.triaged} triaged / {metrics.total_findings} published in 30 days</p></div>
      <div className="rounded border p-4"><h2>Observed audit error rate</h2>
        <strong>{metrics.audited_total ? `${(metrics.validation_error_rate*100).toFixed(1)}%` : 'Not enough data'}</strong>
        <p>{metrics.audited_invalid} invalid / {metrics.audited_total} audited</p></div>
      <div className="rounded border p-4"><h2>Audit sample</h2><p>{sampleIds.length} findings selected.</p>
        <p>These are observed sample statistics, not an unbiased repository-wide estimate.</p></div>
    </div>}
    {loadedRepo && !busy && findings.length === 0 && <p>No published findings in the selected period.</p>}
    {!!findings.length && <label className="block">Finding and PR<select className={inputClass} value={selected}
      onChange={event => { setSelected(event.target.value); setNotes(''); setMessage(''); }}>
      <option value="">Select a finding</option>
      {findings.map(f => <option key={f.finding_id} value={f.finding_id}>
        PR #{f.pr_id} — {f.claim_title || f.fingerprint}{sampleIds.includes(f.finding_id) ? ' (audit sample)' : ''}
      </option>)}
    </select></label>}
    {finding && <div className="space-y-4 rounded border border-slate-700 p-5">
      <h2>{finding.repo_id} · PR #{finding.pr_id}</h2>
      <p>{finding.claim_description}</p><p>{finding.evidence_grade} · {finding.outcome}</p>
      <p className="break-all text-sm">Finding ID: {finding.finding_id}</p>
      <label className="block">Notes<textarea className={inputClass} value={notes} onChange={e => setNotes(e.target.value)} /></label>
      <label className="block">Feedback<select className={inputClass} value={outcome} onChange={e => setOutcome(e.target.value)}>
        {contracts?.outcomes.map(value => <option key={value}>{value}</option>)}
      </select></label>
      <button disabled={busy} className="rounded bg-indigo-600 p-3" onClick={() => void submit()}>Save feedback</button>
      {finding.evidence_grade === 'REPRODUCED' && <div className="space-y-3">
        <h3>Review the recorded reproduction</h3><pre className="overflow-auto text-sm">{finding.generated_test_code}</pre>
        <label>Rejection reason<select className={inputClass} value={reason} onChange={e => setReason(e.target.value)}>
          {contracts?.audit_reasons.filter(v => v !== 'CORRECT_REPRODUCTION').map(value => <option key={value}>{value}</option>)}
        </select></label>
        <button disabled={busy} className="rounded bg-emerald-800 p-3 mr-3" onClick={() => void submit(true)}>Confirm reproduction</button>
        <button disabled={busy} className="rounded bg-rose-800 p-3" onClick={() => void submit(false)}>Reject reproduction</button>
      </div>}
    </div>}
  </section>;
}
