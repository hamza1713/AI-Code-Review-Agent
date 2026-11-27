import { useCallback, useEffect, useState } from 'react';

export const VIEWS = ['review', 'jobs', 'calibration', 'labeling'] as const;
export type View = (typeof VIEWS)[number];

function fromHash(): View {
  const value = window.location.hash.replace(/^#\/?/, '');
  return (VIEWS as readonly string[]).includes(value) ? (value as View) : 'review';
}

/** Keeps the active workspace view in the URL hash so back, forward and reload work. */
export function useHashView() {
  const [view, setView] = useState<View>(fromHash);

  useEffect(() => {
    const sync = () => setView(fromHash());
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);

  const navigate = useCallback((next: View) => {
    if (fromHash() !== next) window.location.hash = `/${next}`;
    setView(next);
  }, []);

  return [view, navigate] as const;
}
