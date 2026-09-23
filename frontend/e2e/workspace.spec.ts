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
  await page.route('**/health', route => route.fulfill({ json: { status: 'healthy', queue: { queued: 0, processing: 0 } } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Start a new review' })).toBeVisible();
}

test('workspace opens without an operator token', async ({ page }) => {
  await connect(page);
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(stored).not.toContain('operator');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Start a new review' })).toBeVisible();
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
  await page.goto('/');
  await expect(page.getByRole('button', { name: /Security findings/ })).toBeVisible();
});

test('copy failures are visible instead of claiming success', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('denied'); } } }));
  await connect(page);
  await page.route('**/api/review', route => route.fulfill({ json: result }));
  await page.getByRole('button', { name: 'Clean Refactor' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await page.getByRole('button', { name: 'Generated Pytest', exact: true }).click();
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
  await page.getByRole('button', { name: /Annotated Code/ }).click();
  await page.getByRole('button', { name: /b.py/ }).click();
  await expect(page.getByText('Only applies to file A.', { exact: true })).not.toBeVisible();
  await expect(page.getByText('y=2', { exact: true })).toBeVisible();
});
