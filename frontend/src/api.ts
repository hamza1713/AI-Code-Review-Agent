let operatorToken = '';
export function setOperatorToken(token: string) { operatorToken = token; }
export async function apiFetch(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  if (operatorToken) headers.set('Authorization', `Bearer ${operatorToken}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && path !== '/api/session') {
    operatorToken = '';
    window.dispatchEvent(new Event('operator-expired'));
  }
  return response;
}
export async function responseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null);
  if (typeof body?.detail === 'string') return body.detail;
  if (response.status === 401) return 'Your operator token was rejected. Reconnect with a valid token.';
  return `The service could not complete this request (${response.status}). Please try again.`;
}

export async function getCalibrationMetrics(repoId?: string, windowDays: number = 30, signal?: AbortSignal) {
  const params = new URLSearchParams();
  if (repoId) params.set('repo_id', repoId);
  params.set('window_days', String(windowDays));
  const response = await apiFetch(`/api/calibration/metrics?${params.toString()}`, {signal});
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}

export async function submitCalibrationFeedback(payload: {
  repo_id: string;
  finding_id: string;
  pr_id?: string;
  fingerprint?: string;
  outcome: string;
  reason?: string;
  source?: string;
}) {
  const response = await apiFetch('/api/calibration/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}

export async function getAuditSample(
  repoId?: string,
  sampleSize: number = 20,
  days: number = 30,
  unauditedOnly: boolean = true,
  signal?: AbortSignal
) {
  const params = new URLSearchParams();
  if (repoId) params.set('repo_id', repoId);
  params.set('sample_size', String(sampleSize));
  params.set('days', String(days));
  params.set('unaudited_only', String(unauditedOnly));
  const response = await apiFetch(`/api/calibration/audit/sample?${params.toString()}`, {signal});
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}

export async function submitAuditDecision(payload: {
  repo_id: string;
  finding_id: string;
  pr_id?: string;
  fingerprint?: string;
  is_valid: boolean;
  reason_code?: string;
  notes?: string;
  audited_by?: string;
}) {
  const response = await apiFetch('/api/calibration/audit/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}
