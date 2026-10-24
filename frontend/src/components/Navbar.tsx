import { Shield, GitPullRequest, Layers, Activity, LogOut } from 'lucide-react';
import type { HealthStatus } from '../types/review';
interface NavbarProps {
  health: HealthStatus | null;
  activeView: 'review' | 'jobs' | 'calibration';
  onViewChange: (view: 'review' | 'jobs' | 'calibration') => void;
  onLogout?: () => void;
}
export function Navbar({ health, activeView, onViewChange, onLogout }: NavbarProps) {
  const isHealthy = Boolean(
    health?.status && ['healthy', 'alive', 'ready', 'ok'].includes(health.status.toLowerCase())
  );
  return <header className="workspace-nav">
    <a href="#main-content" className="skip-link">Skip to workspace</a>
    <div className="nav-inner">
      <div className="brand"><span className="brand-icon"><Shield size={22} /></span><div>Code Review<span className="brand-subtitle">AGENT WORKSPACE</span></div><span className="version-pill">v2.0</span></div>
      <nav aria-label="Main navigation" className="nav-links">
        <button aria-current={activeView === 'review' ? 'page' : undefined} onClick={() => onViewChange('review')}><GitPullRequest size={16} />Reviews</button>
        <button aria-current={activeView === 'jobs' ? 'page' : undefined} onClick={() => onViewChange('jobs')}><Layers size={16} />Job queue{health?.queue?.queued ? <span className="queue-count">{health.queue.queued}</span> : null}</button>
        <button aria-current={activeView === 'calibration' ? 'page' : undefined} onClick={() => onViewChange('calibration')}><Activity size={16} />Calibration</button>
      </nav>
      <div className="connection-status" role="status"><span className={isHealthy ? 'status-dot online' : 'status-dot'} />{isHealthy ? 'Service connected' : 'Service unavailable'}</div>
      {onLogout && <button type="button" onClick={onLogout} className="logout-button"><LogOut size={16} />Log out</button>}
    </div>
  </header>;
}
