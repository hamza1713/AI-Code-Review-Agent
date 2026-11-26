import { Shield, GitPullRequest, Layers, Activity, LogOut, ClipboardCheck, Sun, Moon, Monitor } from 'lucide-react';
import type { HealthStatus } from '../types/review';
import type { View } from '../hooks/useHashView';
import type { ThemePreference } from '../hooks/useTheme';

interface NavbarProps {
  health: HealthStatus | null;
  activeView: View;
  onViewChange: (view: View) => void;
  onLogout?: () => void;
  theme: ThemePreference;
  onCycleTheme: () => void;
}

const THEME_LABEL: Record<ThemePreference, string> = { system: 'System theme', light: 'Light theme', dark: 'Dark theme' };

export function Navbar({ health, activeView, onViewChange, onLogout, theme, onCycleTheme }: NavbarProps) {
  const isHealthy = Boolean(
    health?.status && ['healthy', 'alive', 'ready', 'ok'].includes(health.status.toLowerCase())
  );
  const ThemeIcon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor;
  const link = (view: View, label: string, icon: React.ReactNode, extra?: React.ReactNode) => (
    <button type="button" aria-current={activeView === view ? 'page' : undefined} onClick={() => onViewChange(view)}>
      {icon}{label}{extra}
    </button>
  );
  return <header className="workspace-nav">
    <a href="#main-content" className="skip-link">Skip to workspace</a>
    <div className="nav-inner">
      <div className="brand">
        <span className="brand-icon"><Shield size={20} /></span>
        <div>Code Review<span className="brand-subtitle">AGENT WORKSPACE</span></div>
        <span className="version-pill">v{__APP_VERSION__}</span>
      </div>
      <nav aria-label="Main navigation" className="nav-links">
        {link('review', 'Reviews', <GitPullRequest size={16} />)}
        {link('jobs', 'Job queue', <Layers size={16} />, health?.queue?.queued ? <span className="queue-count">{health.queue.queued}</span> : null)}
        {link('calibration', 'Calibration', <Activity size={16} />)}
        {link('labeling', 'Labeling', <ClipboardCheck size={16} />)}
      </nav>
      <div className="nav-actions">
        <div className="connection-status" role="status"><span className={isHealthy ? 'status-dot online' : 'status-dot'} /><span>{isHealthy ? 'Connected' : 'Service unavailable'}</span></div>
        <button type="button" className="icon-button" onClick={onCycleTheme} aria-label={`${THEME_LABEL[theme]}. Switch theme`} title={THEME_LABEL[theme]}>
          <ThemeIcon size={16} />
        </button>
        {onLogout && <button type="button" onClick={onLogout} className="logout-button"><LogOut size={15} /><span className="hidden sm:inline">Log out</span></button>}
      </div>
    </div>
  </header>;
}
