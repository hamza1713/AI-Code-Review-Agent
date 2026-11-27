import clsx from 'clsx';
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react';

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={clsx('rounded-2xl border border-line bg-surface', className)} {...props} />;
}

type Variant = 'primary' | 'secondary' | 'danger' | 'success' | 'ghost';
const VARIANTS: Record<Variant, string> = {
  primary: 'bg-indigo-600 text-white hover:bg-indigo-500 border-transparent',
  secondary: 'bg-raised text-ink hover:border-indigo-500/60 border-line',
  danger: 'bg-rose-600 text-white hover:bg-rose-500 border-transparent',
  success: 'bg-emerald-600 text-white hover:bg-emerald-500 border-transparent',
  ghost: 'bg-transparent text-ink-muted hover:text-ink hover:bg-raised border-transparent',
};

export function Button({
  variant = 'secondary',
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={props.type ?? 'button'}
      className={clsx(
        'inline-flex items-center justify-center gap-2 rounded-lg border px-4 py-2 text-sm font-medium transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-sm font-medium text-ink">{label}</span>
      {children}
      {hint && <span className="block text-xs text-ink-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  'w-full rounded-lg border border-line bg-canvas px-3 py-2.5 text-sm text-ink placeholder:text-ink-muted focus:border-indigo-500';

export function Kpi({
  label,
  value,
  detail,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'bad';
}) {
  const accent = {
    neutral: 'text-ink',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-rose-300',
  }[tone];
  return (
    <Card className="p-5">
      <p className="text-sm text-ink-muted">{label}</p>
      <p className={clsx('mt-2 text-3xl font-semibold tracking-tight', accent)}>{value}</p>
      {detail && <p className="mt-1.5 text-xs text-ink-muted">{detail}</p>}
    </Card>
  );
}

export function EmptyState({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <Card className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      {icon && <div className="text-ink-muted">{icon}</div>}
      <p className="text-base font-medium text-ink">{title}</p>
      {children && <div className="max-w-md text-sm text-ink-muted">{children}</div>}
    </Card>
  );
}

export function Notice({ tone, children }: { tone: 'error' | 'success' | 'info'; children: ReactNode }) {
  const styles = {
    error: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
    success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
    info: 'border-line bg-raised text-ink-muted',
  }[tone];
  return (
    <div role={tone === 'error' ? 'alert' : 'status'} className={clsx('rounded-xl border px-4 py-3 text-sm', styles)}>
      {children}
    </div>
  );
}

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'border-rose-500/40 bg-rose-500/15 text-rose-200',
  HIGH: 'border-orange-500/40 bg-orange-500/15 text-orange-200',
  MEDIUM: 'border-amber-500/40 bg-amber-500/15 text-amber-200',
  WARNING: 'border-amber-500/40 bg-amber-500/15 text-amber-200',
  LOW: 'border-sky-500/40 bg-sky-500/15 text-sky-200',
  INFO: 'border-slate-500/40 bg-slate-500/15 text-slate-300',
};

export function SeverityPill({ severity, className }: { severity: string; className?: string }) {
  const key = (severity || 'INFO').toUpperCase();
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold tracking-wide',
        SEVERITY_STYLES[key] ?? SEVERITY_STYLES.INFO,
        className,
      )}
    >
      {key}
    </span>
  );
}
