import { useCallback, useEffect, useState } from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';
const KEY = 'review-theme';

function readPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(KEY);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
}

function apply(preference: ThemePreference) {
  const root = document.documentElement;
  if (preference === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', preference);
}

/** Theme choice is a per-browser convenience; 'system' follows the OS setting. */
export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);

  useEffect(() => {
    apply(preference);
    try {
      if (preference === 'system') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, preference);
    } catch {
      // Storage can be unavailable (private mode); the theme still applies for this visit.
    }
  }, [preference]);

  const cycle = useCallback(() => {
    setPreference(current => (current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system'));
  }, []);

  return { preference, setPreference, cycle };
}
