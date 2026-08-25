import React from 'react';
import { Shield, GitPullRequest, Server, Code2, Lock, UserCheck } from 'lucide-react';
import type { HealthStatus } from '../types/review';

interface NavbarProps {
  health: HealthStatus | null;
  activeView: 'review' | 'jobs';
  onViewChange: (view: 'review' | 'jobs') => void;
}

export const Navbar: React.FC<NavbarProps> = ({ health, activeView, onViewChange }) => {
  const isOnline = health?.status === 'healthy';

  return (
    <header className="border-b border-slate-800 bg-[#0d1117]/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Brand Logo & Title */}
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 via-purple-600 to-pink-500 flex items-center justify-center shadow-lg shadow-indigo-500/20 ring-1 ring-white/20">
              <Shield className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="font-bold text-lg text-white tracking-tight">AI Code Review Agent</span>
                <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                  Enterprise v2.0
                </span>
              </div>
              <p className="text-xs text-slate-400 font-medium">Multi-Agent Static & Dynamic Code Intelligence</p>
            </div>
          </div>

          {/* Navigation Views */}
          <div className="flex items-center bg-slate-900/80 p-1 rounded-lg border border-slate-800">
            <button
              onClick={() => onViewChange('review')}
              className={`flex items-center space-x-2 px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
                activeView === 'review'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
              }`}
            >
              <GitPullRequest className="w-3.5 h-3.5" />
              <span>Review Workspace</span>
            </button>
            <button
              onClick={() => onViewChange('jobs')}
              className={`flex items-center space-x-2 px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
                activeView === 'jobs'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
              }`}
            >
              <Server className="w-3.5 h-3.5" />
              <span>Webhook Queue</span>
              {health?.queue?.queued ? (
                <span className="bg-amber-500 text-black font-extrabold text-[10px] px-1.5 py-0.2 rounded-full">
                  {health.queue.queued}
                </span>
              ) : null}
            </button>
          </div>

          {/* Multi-Agent Crew & Backend Status */}
          <div className="hidden md:flex items-center space-x-4">
            {/* Agent badges */}
            <div className="flex items-center space-x-1.5 text-xs text-slate-300">
              <div className="flex items-center space-x-1 px-2 py-1 rounded bg-slate-800/60 border border-slate-700/50" title="Senior Developer Agent: Call Graph & Architecture">
                <Code2 className="w-3 h-3 text-cyan-400" />
                <span className="text-[11px]">Senior Dev</span>
              </div>
              <div className="flex items-center space-x-1 px-2 py-1 rounded bg-slate-800/60 border border-slate-700/50" title="Security Engineer Agent: AppSec & Pattern Audit">
                <Lock className="w-3 h-3 text-rose-400" />
                <span className="text-[11px]">Security Eng</span>
              </div>
              <div className="flex items-center space-x-1 px-2 py-1 rounded bg-slate-800/60 border border-slate-700/50" title="Tech Lead Agent: Governance & Pytest Generator">
                <UserCheck className="w-3 h-3 text-amber-400" />
                <span className="text-[11px]">Tech Lead</span>
              </div>
            </div>

            {/* Health status */}
            <div className="flex items-center space-x-2 px-2.5 py-1 rounded-full bg-slate-900 border border-slate-800">
              <span className={`w-2 h-2 rounded-full ${isOnline ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'}`} />
              <span className="text-xs font-mono text-slate-300">
                {isOnline ? 'Backend Online' : 'Backend Offline'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </header>
  );
};
