import { Shield, GitPullRequest, Layers } from 'lucide-react';
import type { HealthStatus } from '../types/review';
interface NavbarProps {
  health: HealthStatus | null;
  activeView: 'review' | 'jobs';
  onViewChange: (view: 'review' | 'jobs') => void;
}
export function Navbar({ health, activeView, onViewChange }: NavbarProps) {
  return <header className="workspace-nav">
    <a href="#main-content" className="skip-link">Skip to workspace</a>
    <div className="nav-inner">
      <div className="brand"><span className="brand-icon"><Shield size={22} /></span><div>Code Review<span className="brand-subtitle">AGENT WORKSPACE</span></div><span className="version-pill">v2.0</span></div>
      <nav aria-label="Main navigation" className="nav-links">
        <button aria-current={activeView === 'review' ? 'page' : undefined} onClick={() => onViewChange('review')}><GitPullRequest size={16} />Reviews</button>
        <button aria-current={activeView === 'jobs' ? 'page' : undefined} onClick={() => onViewChange('jobs')}><Layers size={16} />Job queue{health?.queue?.queued ? <span className="queue-count">{health.queue.queued}</span> : null}</button>
      </nav>
      <div className="connection-status" role="status"><span className={health?.status === 'healthy' ? 'status-dot online' : 'status-dot'} />{health?.status === 'healthy' ? 'Service connected' : 'Service unavailable'}</div>
    </div>
  </header>;
}
