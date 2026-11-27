import { test, expect, type Page } from '@playwright/test';
const result = {
  verdict: 'COMMENT', confidence_score: 80, summary: 'Review complete.',
  pattern_findings_label: 'Security patterns', pattern_findings: [{ rule_id: 'DEMO', cwe: 'CWE-20', description: 'Check input', severity: 'LOW', file_path: 'a.py', line_number: 1, fix_recommendation: 'Validate input.' }],
  governance_violations: [], inline_comments: [],
  cross_file_impact: { available: false, is_python: true, message: 'No impact found.', impacted_callers: {} },
  reviewed_diff: 'diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+x=1\n',
  scope_note: 'Heuristic review.', generated_unit_tests: 'def test_sum():\n    assert 2 + 2 == 4',
  test_execution: { executed: false, status: 'SKIPPED', evidence_badge: 'UNVERIFIED', summary_message: 'Execution disabled.' },
};
async function connect(page: Page) {
  await page.route('**/jobs/metrics', route => route.fulfill({ json: { queued: 0, processing: 0, failed: 0, completed: 0 } }));
  await page.route('**/health', route => route.fulfill({ json: { status: 'healthy', queue: { queued: 0, processing: 0 } } }));
  await page.route('**/api/review/stream', route => route.abort('connectionfailed'));
  await page.route('**/api/session', route => {
    if (!route.request().headers()['authorization']) {
      return route.fulfill({status:401,json:{detail:'Operator token required'}});
    }
    expect(route.request().headers()['authorization']).toBe('Bearer test-operator');
    return route.fulfill({json:{authenticated:true}});
  });
  await page.goto('/');
  await page.getByLabel('Operator access token').fill('test-operator');
  await page.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(page.getByLabel('Operator access token')).not.toBeVisible();
}

test('coverage and unavailable usage remain explicit', async ({ page }) => {
  await connect(page);
  const response = { ...result, analysis_complete: false, verdict: 'ESCALATE', reviewer_profile_id: 'a'.repeat(64),
    reviewer_profile: { model: 'ollama/pinned-model', application_commit: 'b'.repeat(40) },
    input_snapshot_hash: 'c'.repeat(64), file_coverage: [{ path: 'api/utils.py', status: 'MISSING', reason: 'Full source unavailable' }],
    telemetry: { duration_seconds: 0.25, total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, token_usage_source: 'UNAVAILABLE', estimated_cost_usd: null, model_used: 'ollama/pinned-model' } };
  await page.route('**/api/review', route => route.fulfill({ status: 202, json: { job_id: 'job_coverage', status: 'QUEUED' } }));
  await page.route('**/jobs/job_coverage', route => route.fulfill({ json: { status: 'COMPLETED', result: response } }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  const coverage = page.getByRole('region', { name: 'Review coverage' });
  await expect(coverage).toContainText('Incomplete analysis');
  await expect(coverage).toContainText('api/utils.py');
  await expect(coverage).toContainText('a'.repeat(64));
  await expect(page.getByText('Unavailable', { exact: true }).first()).toBeVisible();
});

test('login is required and credentials stay in memory', async ({ page }) => {
  await connect(page);
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(stored).not.toContain('operator');
  await page.reload();
  await expect(page.getByLabel('Operator access token')).toBeVisible();
});

test('failed submission preserves the draft and navigation stays usable', async ({ page }) => {
  await connect(page);
  await page.route('**/api/review', route => {
    return route.fulfill({ status: 503, json: { detail: 'Workers are busy. Try again shortly.' } });
  });
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  const draft = await page.getByLabel('Your code changes').inputValue();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Workers are busy');
  await expect(page.getByLabel('Your code changes')).toHaveValue(draft);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('upload rejections are visible and invalid selections cannot submit', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'Upload File', exact: true }).click();
  await page.locator('input[type=file]').setInputFiles({ name: 'large.py', mimeType: 'text/plain', buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
  await expect(page.getByRole('alert')).toContainText('no larger than 2 MB');
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('select or drop');
  await page.getByRole('button', { name: 'Upload ZIP', exact: true }).click();
  await page.locator('input[type=file]').setInputFiles({ name: 'wrong.txt', mimeType: 'text/plain', buffer: Buffer.from('text') });
  await expect(page.getByRole('alert')).toContainText('.zip archive');
});

test('durable review returns real counts and LOW filtering', async ({ page }) => {
  await connect(page);
  await page.route('**/api/review', route => {
    expect(route.request().headers()['prefer']).toBe('respond-async');
    return route.fulfill({ status: 202, json: { job_id: 'job_demo', status: 'QUEUED' } });
  });
  await page.route('**/jobs/job_demo', route => route.fulfill({ json: { status: 'COMPLETED', result } }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await page.getByRole('button', { name: /Security findings/ }).click();
  await page.getByRole('button', { name: 'LOW', exact: true }).click();
  await expect(page.getByText('Check input', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('review-job-id'))).toBeNull();
});

test('queue outage is distinguished from empty data and recovers', async ({ page }) => {
  await connect(page);
  await page.route('**/api/webhook/config', route => route.fulfill({ json: { webhook_url: 'https://example.test/webhook/github', secret_configured: true, github_connected: false, test_events_enabled: false } }));
  let failed = true;
  await page.route('**/jobs', route => route.fulfill(failed ? { status: 500, json: { detail: 'Queue unavailable.' } } : { json: { jobs: [] } }));
  await page.getByRole('button', { name: 'Job queue', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Queue unavailable');
  await expect(page.getByText('No jobs match this filter.')).not.toBeVisible();
  failed = false;
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByText('No jobs match this filter.')).toBeVisible();
  await page.getByRole('button', { name: 'ALL', exact: true }).click();
  await expect(page.getByText('No jobs match this filter.')).toBeVisible();
  await expect(page.getByRole('status').last()).toContainText('Last refreshed');
});

test('refresh reconnects to the saved review without resubmitting', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('review-job-id', 'job_resume'));
  await page.route('**/jobs/job_resume', route => route.fulfill({ json: { status: 'COMPLETED', result } }));
  await page.route('**/api/review', () => { throw new Error('Must not resubmit a saved review'); });
  await connect(page);
  await expect(page.getByRole('button', { name: /Security findings/ })).toBeVisible();
});

test('copy failures are visible instead of claiming success', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('denied'); } } }));
  await connect(page);
  await page.route('**/api/review', route => route.fulfill({ json: result }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await page.getByRole('tab', { name: 'Generated tests', exact: true }).click();
  await page.getByRole('button', { name: /Copy/ }).click();
  await expect(page.getByRole('alert')).toContainText('Copy failed');
  await expect(page.getByText('Copied', { exact: true })).not.toBeVisible();
});

test('annotations stay attached to their own file', async ({ page }) => {
  await connect(page);
  const multiFile = { ...result, pattern_findings: [],
    reviewed_diff: 'diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+x=1\ndiff --git a/b.py b/b.py\n@@ -0,0 +1 @@\n+y=2\n',
    inline_comments: [{ path: 'a.py', line: 1, side: 'RIGHT', severity: 'WARNING', comment_body: 'Only applies to file A.' }],
  };
  await page.route('**/api/review', route => route.fulfill({ json: multiFile }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await page.getByRole('tab', { name: /Annotated code/ }).click();
  await page.getByRole('button', { name: /b.py/ }).click();
  await expect(page.getByText('Only applies to file A.', { exact: true })).not.toBeVisible();
  await expect(page.getByText('y=2', { exact: true })).toBeVisible();
});

test('theme choice persists across reloads', async ({ page }) => {
  await connect(page);
  const toggle = page.getByRole('button', { name: /theme\. Switch theme/ });
  await toggle.click(); // system -> light
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await toggle.click(); // light -> dark
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('views live in the URL and support back navigation', async ({ page }) => {
  await page.route('**/api/eval/cases', route => route.fulfill({ status: 404, json: { detail: 'The evaluation corpus is not available on this server.' } }));
  await page.route('**/api/eval/score', route => route.fulfill({ status: 404, json: { detail: 'missing' } }));
  await connect(page);
  await page.getByRole('button', { name: 'Labeling', exact: true }).click();
  await expect(page).toHaveURL(/#\/labeling$/);
  await expect(page.getByText('Labeling is not available on this server')).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel('Your code changes')).toBeVisible();
});

test('review tabs follow the ARIA tablist keyboard pattern', async ({ page }) => {
  await connect(page);
  await page.route('**/api/review', route => route.fulfill({ json: result }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  const first = page.getByRole('tab', { name: 'Full overview' });
  await first.click();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'AI threat radar' })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('End');
  await expect(page.getByRole('tab', { name: 'Generated tests' })).toBeFocused();
  await expect(page.getByRole('tabpanel')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('calibration shows acceptance, latency and noisy rules', async ({ page }) => {
  await connect(page);
  await page.route('**/api/calibration/metrics?**', route => route.fulfill({ json: {
    total_findings: 12, triaged: 10, false_positive_rate: 0.2, acceptance_rate: 0.8,
    audited_total: 0, audited_invalid: 0, validation_error_rate: 0 } }));
  await page.route('**/api/calibration/findings?**', route => route.fulfill({ json: { findings: [] } }));
  await page.route('**/api/calibration/contracts', route => route.fulfill({ json: { outcomes: ['FALSE_POSITIVE'], audit_reasons: [] } }));
  await page.route('**/api/calibration/audit/sample?**', route => route.fulfill({ json: { findings: [] } }));
  await page.route('**/api/calibration/rules?**', route => route.fulfill({ json: { rules: [
    { rule_id: 'SEC-NOISY', total: 10, triaged: 10, confirmed_real: 6, false_positives: 4, false_positive_rate: 0.4, acceptance_rate: 0.6, on_probation: true },
  ] } }));
  await page.route('**/api/telemetry/latency?**', route => route.fulfill({ json: { runs: 5, latency_p50_seconds: 4.2, latency_p95_seconds: 9.8 } }));
  await page.getByRole('button', { name: 'Calibration', exact: true }).click();
  await page.getByLabel('Repository').fill('acme/api');
  await page.getByRole('button', { name: /Load metrics/ }).click();
  await expect(page.getByText('80.0%')).toBeVisible();
  await expect(page.getByText('4.2s / 9.8s')).toBeVisible();
  await expect(page.getByRole('row', { name: /SEC-NOISY/ })).toContainText('Probation');
});

test('labeling saves a reviewer label and updates progress', async ({ page }) => {
  let labeled = 0;
  const listing = () => ({
    split: 'development', cohorts: ['clean', 'hard_negative', 'defect'], reviewers: { a: labeled ? 'Ana Reviewer' : null, b: null },
    progress: {
      reviewer_a: { total: 1, labeled, adjudicated: 0, needs_adjudication: 0, unreviewed: 1 - labeled },
      reviewer_b: { total: 1, labeled: 0, adjudicated: 0, needs_adjudication: 0, unreviewed: 1 },
      adjudication: { total: 1, labeled: 0, adjudicated: 0, needs_adjudication: 0, unreviewed: 1 },
    },
    predictions_available: true,
    cases: [{ id: 'flask-1', predicted_findings: 0, reviewer_a: labeled ? 'LABELED' : 'UNREVIEWED', reviewer_b: 'UNREVIEWED', adjudication: 'UNREVIEWED' }],
  });
  await page.route('**/api/eval/cases', route => route.fulfill({ json: listing() }));
  await page.route('**/api/eval/score', route => route.fulfill({ json: { ready: false, progress: listing().progress.adjudication,
    agreement: { cases_compared: 0, cohort_agreement: null, cohort_kappa: null, finding_agreement: null } } }));
  await page.route('**/api/eval/cases/flask-1?**', route => route.fulfill({ json: {
    id: 'flask-1', diff: 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n',
    predicted_findings: [], label: { status: 'UNREVIEWED', cohort: null, rationale: '', findings: [] } } }));
  await page.route('**/api/eval/labels/a/flask-1', route => {
    const body = route.request().postDataJSON();
    expect(body).toMatchObject({ reviewer_name: 'Ana Reviewer', cohort: 'clean' });
    labeled = 1;
    return route.fulfill({ json: { saved: true } });
  });
  await connect(page);
  await page.getByRole('button', { name: 'Labeling', exact: true }).click();
  await page.getByLabel('Your name').fill('Ana Reviewer');
  await page.getByRole('button', { name: /flask-1/ }).click();
  await page.getByText('Clean', { exact: true }).click();
  await page.getByLabel('Rationale').fill('Version bump only.');
  await page.getByRole('button', { name: 'Save label' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Label saved.' })).toBeVisible();
  await expect(page.getByText('1/1')).toBeVisible();
});
