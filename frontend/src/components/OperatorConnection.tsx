import { useState } from 'react';
import { KeyRound, ArrowRight, Shield, Lock } from 'lucide-react';
import { apiFetch, responseError, setOperatorToken } from '../api';
import { Button, Card, Notice, inputClass } from '../ui/primitives';

export function OperatorConnection({ onConnected }: { onConnected: () => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const connect = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError('');
    setOperatorToken(token.trim());
    try {
      const response = await apiFetch('/api/session');
      if (!response.ok) throw new Error(await responseError(response));
      setToken('');
      onConnected();
    } catch (reason) {
      setOperatorToken('');
      setError(reason instanceof Error ? reason.message : 'Unable to connect.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-12">
      <div className="mb-8 flex items-center gap-3 text-lg font-semibold">
        <span className="brand-icon"><Shield size={20} /></span>
        Code Review Agent
      </div>
      <Card className="w-full max-w-md p-7 shadow-xl shadow-black/10">
        <form className="space-y-5" onSubmit={connect}>
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-500/15 text-indigo-300">
            <KeyRound size={22} />
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Connect to your workspace</h1>
            <p className="mt-2 text-sm leading-relaxed text-ink-muted">
              Enter the operator token set by your server administrator. It grants access to this deployment's reviews and jobs.
            </p>
          </div>
          <div className="space-y-2">
            <label htmlFor="operator-token" className="block text-sm font-medium">Operator access token</label>
            <input
              id="operator-token"
              name="operator-token"
              type="password"
              autoComplete="current-password"
              required
              value={token}
              onChange={event => setToken(event.target.value)}
              className={inputClass}
            />
          </div>
          <p className="flex items-center gap-2 text-xs text-ink-muted">
            <Lock size={13} /> Kept in memory only. Refreshing this page disconnects your session.
          </p>
          {error && <Notice tone="error">{error}</Notice>}
          <Button type="submit" variant="primary" disabled={loading} className="w-full py-3">
            {loading ? 'Connecting…' : 'Connect'}
            <ArrowRight size={16} />
          </Button>
        </form>
      </Card>
    </div>
  );
}
