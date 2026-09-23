let operatorToken = '';
export function setOperatorToken(token: string) { operatorToken = token; }
export function apiFetch(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  if (operatorToken) headers.set('Authorization', `Bearer ${operatorToken}`);
  return fetch(path, { ...options, headers });
}
export async function responseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null);
  if (typeof body?.detail === 'string') return body.detail;
  if (response.status === 401) return 'Your operator token was rejected. Reconnect with a valid token.';
  return `The service could not complete this request (${response.status}). Please try again.`;
}
