import { useCallback, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import { CheckCircle2, ClipboardCheck, Eye, EyeOff, GitMerge, Plus, Trash2 } from 'lucide-react';
import { getJson, sendJson } from '../api';
import { Button, Card, EmptyState, Field, Kpi, Notice, SeverityPill, inputClass } from '../ui/primitives';

type Role = 'a' | 'b' | 'adjudication';
type Cohort = 'clean' | 'hard_negative' | 'defect';

interface GoldFinding {
  file_path: string; line_number: number; rule_ids: string[]; cwe?: string | null; severity: string; note?: string;
}
interface CaseRow { id: string; predicted_findings: number; reviewer_a: string; reviewer_b: string; adjudication: string }
interface Progress { total: number; labeled: number; adjudicated: number; needs_adjudication: number; unreviewed: number }
interface Listing {
  cases: CaseRow[]; reviewers: { a: string | null; b: string | null };
  progress: { reviewer_a: Progress; reviewer_b: Progress; adjudication: Progress };
}
interface Candidate { cohort: Cohort; findings: GoldFinding[]; rationale: string }
interface CaseLabel {
  status: string; cohort: Cohort | null; rationale: string; findings: GoldFinding[];
  reviewers?: string[]; adjudicator?: string | null; candidates?: { a: Candidate; b: Candidate };
}
interface CaseDetail {
  id: string; diff: string; label: CaseLabel;
  predicted_findings: { file_path?: string; line_number?: number; rule_id?: string; cwe?: string; severity?: string; description?: string }[];
}
interface ScoreState {
  ready: boolean; progress: Progress;
  agreement: { cases_compared: number; cohort_agreement: number | null; cohort_kappa: number | null; finding_agreement: number | null };
  score?: {
    precision: number | null; recall: number | null; f1: number | null; precision_ci95: number[] | null; recall_ci95: number[] | null;
    clean_pr_false_alarm_rate: number | null; clean_pr_false_alarm_rate_ci95: number[] | null; cases: number;
    tp: number; fp: number; fn: number;
  };
}

interface DiffLine { kind: 'add' | 'del' | 'ctx' | 'hunk' | 'file'; text: string; file?: string; line?: number }

function parseDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let file = '';
  let next = 0;
  for (const raw of diff.split('\n')) {
    if (raw.startsWith('diff --git')) { out.push({ kind: 'file', text: raw }); continue; }
    if (raw.startsWith('+++ ')) { file = raw.slice(4).replace(/^b\//, ''); out.push({ kind: 'file', text: file }); continue; }
    if (raw.startsWith('--- ') || raw.startsWith('index ')) continue;
    const hunk = /^@@ -\d+(?:,\d+)? \+(\d+)/.exec(raw);
    if (hunk) { next = Number(hunk[1]); out.push({ kind: 'hunk', text: raw }); continue; }
    if (raw.startsWith('+')) out.push({ kind: 'add', text: raw.slice(1), file, line: next++ });
    else if (raw.startsWith('-')) out.push({ kind: 'del', text: raw.slice(1), file });
    else if (raw.startsWith(' ')) out.push({ kind: 'ctx', text: raw.slice(1), file, line: next++ });
  }
  return out.filter(l => l.kind !== 'file' || !l.text.startsWith('diff --git'));
}

const STATUS_STYLE: Record<string, string> = {
  LABELED: 'bg-emerald-500/15 text-emerald-200 border-emerald-500/40',
  ADJUDICATED: 'bg-emerald-500/15 text-emerald-200 border-emerald-500/40',
  NEEDS_ADJUDICATION: 'bg-amber-500/15 text-amber-200 border-amber-500/40',
  UNREVIEWED: 'bg-raised text-ink-muted border-line',
};
const COHORTS: { id: Cohort; label: string; help: string }[] = [
  { id: 'clean', label: 'Clean', help: 'No real defect in the changed lines' },
  { id: 'hard_negative', label: 'Hard negative', help: 'Looks risky but is safe in context' },
  { id: 'defect', label: 'Defect', help: 'At least one real issue' },
];
const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const ci = (v: number[] | null | undefined) => (v ? `95% CI ${pct(v[0])} – ${pct(v[1])}` : 'Not enough data');

export function LabelingWorkbench() {
  const [role, setRole] = useState<Role>('a');
  const [listing, setListing] = useState<Listing | null>(null);
  const [score, setScore] = useState<ScoreState | null>(null);
  const [caseId, setCaseId] = useState<string>('');
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [name, setName] = useState('');
  const [cohort, setCohort] = useState<Cohort | null>(null);
  const [rationale, setRationale] = useState('');
  const [findings, setFindings] = useState<GoldFinding[]>([]);
  const [draft, setDraft] = useState<GoldFinding | null>(null);
  const [showPredictions, setShowPredictions] = useState(false);
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [unavailable, setUnavailable] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [cases, scoreState] = await Promise.all([getJson('/api/eval/cases', signal), getJson('/api/eval/score', signal)]);
      if (signal?.aborted) return null;
      setListing(cases); setScore(scoreState); setUnavailable(null);
      return cases as Listing;
    } catch (error) {
      if (!signal?.aborted) setUnavailable(error instanceof Error ? error.message : 'The labeling service is unavailable.');
      return null;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      const cases = await refresh(controller.signal);
      // Pre-fill reviewer A's name so a returning reviewer does not retype it.
      if (cases) setName(current => current || (cases.reviewers.a ?? ''));
    })();
    return () => controller.abort();
  }, [refresh]);

  const changeRole = (next: Role) => {
    setRole(next);
    setDetail(null); setCaseId(''); setNotice(null); setDraft(null);
    setName(next === 'adjudication' ? '' : listing?.reviewers[next] ?? '');
  };

  const visibleCases = useMemo(() => {
    if (!listing) return [];
    if (role !== 'adjudication') return listing.cases;
    return listing.cases.filter(c => c.adjudication === 'NEEDS_ADJUDICATION' || c.adjudication === 'ADJUDICATED');
  }, [listing, role]);

  const openCase = async (id: string) => {
    setCaseId(id); setNotice(null); setDraft(null); setBusy(true);
    try {
      const data: CaseDetail = await getJson(`/api/eval/cases/${encodeURIComponent(id)}?reviewer=${role}`);
      setDetail(data);
      setCohort(data.label.cohort);
      setRationale(data.label.rationale || '');
      setFindings(data.label.findings || []);
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not open this case.' });
    } finally { setBusy(false); }
  };

  const statusFor = (row: CaseRow) => (role === 'a' ? row.reviewer_a : role === 'b' ? row.reviewer_b : row.adjudication);

  const save = async () => {
    if (!detail || !cohort) return;
    setBusy(true); setNotice(null);
    try {
      if (role === 'adjudication') {
        await sendJson(`/api/eval/adjudication/${encodeURIComponent(detail.id)}`, 'PUT', { adjudicator: name, cohort, rationale, findings });
      } else {
        await sendJson(`/api/eval/labels/${role}/${encodeURIComponent(detail.id)}`, 'PUT', { reviewer_name: name, cohort, rationale, findings });
      }
      const cases = await refresh();
      setNotice({ tone: 'success', text: 'Label saved.' });
      const order = (cases ?? listing)?.cases ?? [];
      const nextCase = order.find(c => c.id !== detail.id && statusFor(c) === 'UNREVIEWED');
      if (nextCase && role !== 'adjudication') void openCase(nextCase.id);
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not save the label.' });
    } finally { setBusy(false); }
  };

  const merge = async () => {
    setBusy(true); setNotice(null);
    try {
      const result = await sendJson('/api/eval/merge', 'POST');
      await refresh();
      setNotice({ tone: 'success', text: `Merged. ${result.progress.adjudicated} adjudicated, ${result.progress.needs_adjudication} need a third reviewer.` });
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not merge labels.' });
    } finally { setBusy(false); }
  };

  const lines = useMemo(() => (detail ? parseDiff(detail.diff) : []), [detail]);
  const predictedAt = useMemo(() => {
    const keyed = new Map<string, string[]>();
    for (const p of detail?.predicted_findings ?? []) {
      const key = `${p.file_path}:${p.line_number}`;
      keyed.set(key, [...(keyed.get(key) ?? []), p.rule_id || p.cwe || 'finding']);
    }
    return keyed;
  }, [detail]);
  const labeledAt = new Set(findings.map(f => `${f.file_path}:${f.line_number}`));

  if (unavailable) {
    return <EmptyState title="Labeling is not available on this server" icon={<ClipboardCheck size={28} />}>{unavailable}<p className="mt-2">Labeling is a development step: production images do not ship the evaluation corpus.</p></EmptyState>;
  }
  if (!listing) return <p className="p-8 text-sm text-ink-muted">Loading the evaluation corpus…</p>;

  const prog = listing.progress;
  return <section className="space-y-6">
    <header className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
      <div>
        <p className="eyebrow">MEASURED QUALITY</p>
        <h1 className="text-2xl font-semibold tracking-tight">Human labeling: development split</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-muted">Two people label each of the {prog.reviewer_a.total} real open-source pull requests independently. Agreements are adjudicated automatically; a third person settles disagreements. See docs/LABELING_GUIDE.md.</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" disabled={busy || !prog.reviewer_a.labeled || !prog.reviewer_b.labeled} onClick={() => void merge()}>
          <GitMerge size={15} />Merge labels
        </Button>
      </div>
    </header>

    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Kpi label={`Reviewer A${listing.reviewers.a ? ` · ${listing.reviewers.a}` : ''}`} value={`${prog.reviewer_a.labeled}/${prog.reviewer_a.total}`} detail="cases labeled" />
      <Kpi label={`Reviewer B${listing.reviewers.b ? ` · ${listing.reviewers.b}` : ''}`} value={`${prog.reviewer_b.labeled}/${prog.reviewer_b.total}`} detail="cases labeled" />
      <Kpi label="Adjudicated" value={`${prog.adjudication.adjudicated}/${prog.adjudication.total}`}
        detail={`${prog.adjudication.needs_adjudication} waiting for a third reviewer`} tone={prog.adjudication.needs_adjudication ? 'warn' : 'neutral'} />
      <Kpi label="Cohort agreement (κ)" value={score?.agreement.cohort_kappa == null ? '—' : score.agreement.cohort_kappa.toFixed(2)}
        detail={score?.agreement.cases_compared ? `${pct(score.agreement.cohort_agreement)} raw agreement over ${score.agreement.cases_compared} cases` : 'Needs both reviewers'} />
    </div>

    {score?.ready && score.score && <Card className="p-5">
      <div className="mb-4 flex items-center gap-2"><CheckCircle2 className="text-emerald-400" size={18} /><h2 className="text-sm font-semibold">Published quality, {score.score.cases} adjudicated PRs (deterministic mode)</h2></div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label="Precision" value={pct(score.score.precision)} detail={ci(score.score.precision_ci95)} />
        <Kpi label="Recall" value={pct(score.score.recall)} detail={ci(score.score.recall_ci95)} />
        <Kpi label="F1" value={pct(score.score.f1)} detail={`${score.score.tp} TP · ${score.score.fp} FP · ${score.score.fn} FN`} />
        <Kpi label="Clean-PR false alarms" value={pct(score.score.clean_pr_false_alarm_rate)} detail={ci(score.score.clean_pr_false_alarm_rate_ci95)} />
      </div>
    </Card>}

    <Card className="flex flex-col gap-4 p-4 md:flex-row md:items-end">
      <div className="md:w-56">
        <Field label="I am labeling as">
          <select className={inputClass} value={role} onChange={e => changeRole(e.target.value as Role)}>
            <option value="a">Reviewer A</option>
            <option value="b">Reviewer B</option>
            <option value="adjudication">Adjudicator (third person)</option>
          </select>
        </Field>
      </div>
      <div className="flex-1">
        <Field label={role === 'adjudication' ? 'Adjudicator name' : 'Your name'} hint="Use your real name. Reviewer A, reviewer B and the adjudicator must be different people.">
          <input className={inputClass} value={name} onChange={e => setName(e.target.value)} placeholder="First and last name" />
        </Field>
      </div>
    </Card>

    {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}

    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
      <Card className="max-h-[70vh] overflow-y-auto p-2">
        <p className="px-2 py-1.5 text-xs font-semibold text-ink-muted">{visibleCases.length} cases</p>
        {visibleCases.length === 0 && <p className="px-2 py-3 text-sm text-ink-muted">Nothing to adjudicate yet. Merge labels first.</p>}
        <ul>{visibleCases.map(row => <li key={row.id}>
          <button type="button" onClick={() => void openCase(row.id)} aria-current={row.id === caseId ? 'true' : undefined}
            className={clsx('flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-sm',
              row.id === caseId ? 'bg-indigo-500/15 text-ink' : 'text-ink-muted hover:bg-raised hover:text-ink')}>
            <span className="font-mono text-xs">{row.id}</span>
            <span className={clsx('rounded border px-1.5 py-0.5 text-xs font-medium', STATUS_STYLE[statusFor(row)] ?? STATUS_STYLE.UNREVIEWED)}>
              {statusFor(row).replace('_', ' ').toLowerCase()}
            </span>
          </button>
        </li>)}</ul>
      </Card>

      {!detail ? <EmptyState title="Choose a case to start" icon={<ClipboardCheck size={28} />}>
        Read the diff, click any changed line to record a finding, then choose a cohort and save.
      </EmptyState> : <div className="space-y-4">
        {role === 'adjudication' && detail.label.candidates && <div className="grid gap-3 md:grid-cols-2">
          {(['a', 'b'] as const).map(slot => {
            const c = detail.label.candidates![slot];
            return <Card key={slot} className="p-4 text-sm">
              <p className="text-xs font-semibold text-ink-muted">Reviewer {slot.toUpperCase()} · {detail.label.reviewers?.[slot === 'a' ? 0 : 1]}</p>
              <p className="mt-1 font-medium">{c.cohort}</p>
              <p className="mt-1 text-ink-muted">{c.rationale}</p>
              <ul className="mt-2 space-y-1 text-xs">{c.findings.map((f, i) => <li key={i} className="font-mono">{f.file_path}:{f.line_number} {f.rule_ids.join(',')} {f.cwe ?? ''}</li>)}</ul>
            </Card>;
          })}
        </div>}

        <Card className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="font-mono text-sm">{detail.id}</h2>
            <button type="button" onClick={() => setShowPredictions(v => !v)} aria-pressed={showPredictions}
              className="inline-flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink">
              {showPredictions ? <Eye size={14} /> : <EyeOff size={14} />}
              {showPredictions ? 'Hide' : 'Show'} tool predictions ({detail.predicted_findings.length})
            </button>
          </div>
          <div className="max-h-[55vh] overflow-auto font-mono text-xs leading-6">
            {lines.map((line, i) => {
              if (line.kind === 'file') return <div key={i} className="sticky top-0 z-10 border-y border-line bg-raised px-4 py-1 font-semibold text-ink">{line.text}</div>;
              if (line.kind === 'hunk') return <div key={i} className="bg-indigo-500/10 px-4 text-indigo-300">{line.text}</div>;
              const key = `${line.file}:${line.line}`;
              const predicted = showPredictions && line.line ? predictedAt.get(key) : undefined;
              const clickable = line.kind !== 'del' && !!line.line;
              return <div key={i} className={clsx('group flex',
                line.kind === 'add' && 'bg-emerald-500/10', line.kind === 'del' && 'bg-rose-500/10',
                labeledAt.has(key) && 'ring-1 ring-inset ring-indigo-400')}>
                <button type="button" disabled={!clickable} title={clickable ? 'Add a finding on this line' : undefined}
                  onClick={() => clickable && setDraft({ file_path: line.file!, line_number: line.line!, rule_ids: [], cwe: '', severity: 'MEDIUM', note: '' })}
                  className="w-14 shrink-0 select-none border-r border-line pr-2 text-right text-ink-muted enabled:hover:bg-indigo-500/20 enabled:hover:text-ink disabled:cursor-default">
                  {line.line ?? ''}
                </button>
                <span className="w-5 shrink-0 text-center text-ink-muted">{line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '}</span>
                <span className="whitespace-pre pr-4 text-ink">{line.text}</span>
                {predicted && <span className="ml-auto shrink-0 self-center pr-3 text-xs text-amber-300">tool: {predicted.join(', ')}</span>}
              </div>;
            })}
          </div>
        </Card>

        {draft && <Card className="space-y-3 border-indigo-500/50 p-4">
          <p className="text-sm font-semibold">New finding at <span className="font-mono">{draft.file_path}:{draft.line_number}</span></p>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Rule IDs" hint="Comma-separated, e.g. SEC-001, B608"><input className={inputClass}
              value={draft.rule_ids.join(', ')} onChange={e => setDraft({ ...draft, rule_ids: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></Field>
            <Field label="CWE" hint="Optional, e.g. CWE-89"><input className={inputClass} value={draft.cwe ?? ''} onChange={e => setDraft({ ...draft, cwe: e.target.value })} /></Field>
            <Field label="Severity"><select className={inputClass} value={draft.severity} onChange={e => setDraft({ ...draft, severity: e.target.value })}>
              {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'].map(s => <option key={s}>{s}</option>)}
            </select></Field>
          </div>
          <Field label="Note"><input className={inputClass} value={draft.note ?? ''} onChange={e => setDraft({ ...draft, note: e.target.value })} /></Field>
          <div className="flex gap-2">
            <Button variant="primary" disabled={!draft.rule_ids.length && !draft.cwe}
              onClick={() => { setFindings([...findings, { ...draft, cwe: draft.cwe || null }]); setDraft(null); if (cohort !== 'defect') setCohort('defect'); }}>
              <Plus size={15} />Add finding
            </Button>
            <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
          </div>
        </Card>}

        <Card className="space-y-4 p-4">
          <fieldset>
            <legend className="mb-2 text-sm font-medium">Cohort</legend>
            <div className="grid gap-2 sm:grid-cols-3">{COHORTS.map(option => <label key={option.id}
              className={clsx('cursor-pointer rounded-xl border p-3 text-sm', cohort === option.id ? 'border-indigo-500 bg-indigo-500/10' : 'border-line hover:bg-raised')}>
              <input type="radio" name="cohort" className="sr-only" checked={cohort === option.id} onChange={() => setCohort(option.id)} />
              <span className="font-medium">{option.label}</span>
              <span className="mt-0.5 block text-xs text-ink-muted">{option.help}</span>
            </label>)}</div>
          </fieldset>

          <div>
            <p className="mb-2 text-sm font-medium">Findings ({findings.length})</p>
            {findings.length === 0 ? <p className="text-xs text-ink-muted">Click a line number in the diff to add one.</p>
              : <ul className="space-y-1.5">{findings.map((f, i) => <li key={i} className="flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-xs">
                <SeverityPill severity={f.severity} />
                <span className="font-mono">{f.file_path}:{f.line_number}</span>
                <span className="text-ink-muted">{[...f.rule_ids, f.cwe].filter(Boolean).join(' · ')}</span>
                <button type="button" aria-label={`Remove finding at line ${f.line_number}`} className="ml-auto text-ink-muted hover:text-rose-300"
                  onClick={() => setFindings(findings.filter((_, j) => j !== i))}><Trash2 size={14} /></button>
              </li>)}</ul>}
          </div>

          <Field label="Rationale" hint="One or two sentences on why.">
            <textarea className={inputClass} rows={3} value={rationale} onChange={e => setRationale(e.target.value)} />
          </Field>
          <div className="flex justify-end">
            <Button variant="primary" disabled={busy || !cohort || !rationale.trim() || !name.trim()} onClick={() => void save()}>
              <CheckCircle2 size={15} />{role === 'adjudication' ? 'Save decision' : 'Save label'}
            </Button>
          </div>
        </Card>
      </div>}
    </div>
  </section>;
}
