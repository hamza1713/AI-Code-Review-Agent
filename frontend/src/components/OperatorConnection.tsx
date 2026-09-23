import { useState } from 'react';
import { KeyRound, ArrowRight } from 'lucide-react';
import { apiFetch, responseError, setOperatorToken } from '../api';
export function OperatorConnection({ onConnected }: { onConnected: () => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  return <form className="input-panel max-w-lg mx-auto my-12 rounded-2xl border p-7 space-y-5" onSubmit={async event => {
    event.preventDefault(); setLoading(true); setError('');
    setOperatorToken(token.trim());
    try {
      const response = await apiFetch('/api/session');
      if (!response.ok) throw new Error(await responseError(response));
      setToken(''); onConnected();
    } catch (reason) {
      setOperatorToken(''); setError(reason instanceof Error ? reason.message : 'Unable to connect.');
    } finally { setLoading(false); }
  }}>
    <KeyRound className="text-indigo-300" size={28} />
    <div><h1 className="text-2xl font-semibold">Connect to your workspace</h1><p className="text-sm text-slate-400 mt-3 leading-relaxed">Enter the operator token configured by your server administrator. It grants access to this deployment’s reviews and jobs.</p></div>
    <div><label htmlFor="operator-token" className="block text-sm mb-2">Operator access token</label><input id="operator-token" type="password" autoComplete="off" required value={token} onChange={event => setToken(event.target.value)} className="w-full border border-slate-600 rounded-lg p-3 bg-slate-950" /></div>
    <p className="text-xs text-slate-400">Kept in memory only. Refreshing this page disconnects your session.</p>
    {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
    <button type="submit" disabled={loading} className="flex items-center justify-center gap-2 rounded-lg bg-indigo-600 px-5 py-3 w-full disabled:opacity-50">{loading ? 'Connecting…' : 'Connect'}<ArrowRight size={16} /></button>
  </form>;
}
