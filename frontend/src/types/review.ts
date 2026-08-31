// TypeScript interfaces for Code Review Agent UI

export type Severity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type RuleSeverity = 'BLOCKING' | 'WARNING' | 'INFO';
export type Verdict = 'APPROVE' | 'REQUEST CHANGES' | 'ESCALATE' | 'COMMENT';

export interface SastFinding {
  rule_id: string;
  cwe: string;
  description: string;
  severity: Severity;
  file_path: string;
  line_number: number;
  snippet?: string;
  fix_recommendation: string;
}

export interface RuleViolation {
  rule_id: string;
  rule_name: string;
  severity: RuleSeverity;
  file_path: string;
  line_number: number;
  description: string;
  suggested_fix: string;
}

export interface InlineComment {
  path: string;
  line: number;
  side: string;
  severity: string;
  comment_body: string;
  why?: string;
  suggestion_code?: string;
}

export interface CrossFileImpactSummary {
  available: boolean;
  is_python: boolean;
  message: string;
  impacted_callers: Record<string, string[]>;
}

export interface TelemetryMetrics {
  duration_seconds: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  model_used: string;
  sast_findings_count: number;
  rule_violations_count: number;
  inline_comments_count: number;
  final_verdict: string;
}

export interface TraceNode {
  id: string;
  title: string;
  stage: string;
  agent_name?: string | null;
  status: 'RUNNING' | 'COMPLETED' | 'FAILED' | 'SKIPPED';
  started_at: number;
  ended_at?: number | null;
  duration_ms?: number | null;
  details?: Record<string, unknown>;
  children?: TraceNode[];
}

export interface TraceData {
  trace_id: string;
  started_at: number;
  duration_ms: number;
  nodes: TraceNode[];
}

export interface ReviewAPIResponse {
  verdict: string;
  confidence_score: number;
  summary: string;
  full_report?: string | null;
  pattern_findings_label: string;
  pattern_findings: SastFinding[];
  governance_violations: RuleViolation[];
  cross_file_impact: CrossFileImpactSummary;
  generated_unit_tests?: string | null;
  inline_comments: InlineComment[];
  telemetry?: TelemetryMetrics | null;
  trace?: TraceData | null;
  reviewed_diff?: string | null;
  scope_note: string;
}



export interface WebhookJob {
  job_id: string;
  pr_identifier: string;
  status: 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'RETRYING';
  created_at: string | number;
  updated_at?: string | number | null;
  started_at?: string | number | null;
  completed_at?: string | number | null;
  attempts?: number;
  max_retries?: number;
  retry_count?: number;
  last_error?: string | null;
  error_message?: string | null;
  payload?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
}


export interface HealthStatus {
  status: string;
  service: string;
  version: string;
  queue: {
    queued: number;
    processing: number;
  };
}
