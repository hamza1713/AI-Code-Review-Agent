// Regenerates README screenshots in docs/images. Skipped in normal runs:
//   CAPTURE_SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts --project=desktop
// The review result is a real deterministic run of samples/benchmarks/complex_multi_issue.diff
// (e2e/fixtures/review-result.json). Calibration numbers are illustrative sample data.
import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const root = join(import.meta.dirname, '..', '..');
const images = join(root, 'docs', 'images');
const result = JSON.parse(readFileSync(join(import.meta.dirname, 'fixtures', 'review-result.json'), 'utf8'));
const corpus = join(root, 'samples', 'evaluation', 'public-pr-v2');
const manifest = JSON.parse(readFileSync(join(corpus, 'inputs.json'), 'utf8'));
const devCases = manifest.cases.filter((c: { split: string }) => c.split === 'development');

test.skip(!process.env.CAPTURE_SCREENSHOTS, 'Set CAPTURE_SCREENSHOTS=1 to regenerate README images.');
test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

async function open(page: Page, theme: 'light' | 'dark', hash = '') {
  await page.addInitScript(t => localStorage.setItem('review-theme', t), theme);
  await page.route('**/health', r => r.fulfill({ json: { status: 'healthy', queue: { queued: 0, processing: 0 } } }));
  await page.route('**/api/session', r => r.fulfill({ json: { authenticated: true } }));
  await page.route('**/api/review/stream', r => r.abort('connectionfailed'));
  await page.goto(`/${hash}`);
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
}

async function runReview(page: Page) {
  await page.route('**/api/review', r => r.fulfill({ json: result }));
  await page.getByRole('button', { name: 'SQLi & Auth PR' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Full overview' })).toBeVisible();
}

const shot = (page: Page, name: string, fullPage = false) =>
  page.screenshot({ path: join(images, name), fullPage, animations: 'disabled' });

for (const theme of ['dark', 'light'] as const) {
  test(`workspace landing (${theme})`, async ({ page }) => {
    await open(page, theme);
    await shot(page, theme === 'dark' ? '01_workspace_landing.png' : '06_workspace_light.png');
  });
}

test('verification pipeline in progress', async ({ page }) => {
  await open(page, 'dark');
  await page.route('**/api/review', r => r.fulfill({ status: 202, json: { job_id: 'job_shot', status: 'QUEUED' } }));
  await page.route('**/jobs/job_shot', r => r.fulfill({ json: { status: 'PROCESSING', current_stage: 'LLM_REASONING', stage_progress: 62 } }));
  await page.getByRole('button', { name: 'SQLi & Auth PR' }).click();
  await page.getByRole('button', { name: 'Review code', exact: true }).click();
  await page.waitForTimeout(2500);
  await shot(page, '02_verification_pipeline.png');
});

test('threat radar and verdict', async ({ page }) => {
  await open(page, 'dark');
  await runReview(page);
  await page.getByRole('tab', { name: 'AI threat radar' }).click();
  await page.getByRole('tablist', { name: 'Review sections' }).evaluate(el => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 90));
  await page.waitForTimeout(400);
  await shot(page, '03_threat_radar.png');
});

test('annotated code', async ({ page }) => {
  await open(page, 'light');
  await runReview(page);
  await page.getByRole('tab', { name: /Annotated code/ }).click();
  await page.getByRole('tablist', { name: 'Review sections' }).evaluate(el => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 90));
  await page.waitForTimeout(600);
  await shot(page, '05_live_annotated_code.png');
});

test('calibration view', async ({ page }) => {
  await page.route('**/api/calibration/metrics?**', r => r.fulfill({ json: {
    total_findings: 148, triaged: 96, false_positive_rate: 0.115, acceptance_rate: 0.885,
    audited_total: 20, audited_invalid: 1, validation_error_rate: 0.05 } }));
  await page.route('**/api/calibration/findings?**', r => r.fulfill({ json: { findings: [{
    finding_id: 'f-1', repo_id: 'acme/payments-api', pr_id: '412', fingerprint: 'fp-1', claim_title: 'Debug print in auth path',
    claim_description: 'print() logs the username during authentication.', evidence_grade: 'HEURISTIC', generated_test_code: '', outcome: 'PENDING' }] } }));
  await page.route('**/api/calibration/contracts', r => r.fulfill({ json: { outcomes: [], audit_reasons: [] } }));
  await page.route('**/api/calibration/audit/sample?**', r => r.fulfill({ json: { findings: [] } }));
  await page.route('**/api/telemetry/latency?**', r => r.fulfill({ json: { runs: 96, latency_p50_seconds: 41.2, latency_p95_seconds: 118.6 } }));
  await page.route('**/api/calibration/rules?**', r => r.fulfill({ json: { rules: [
    { rule_id: 'GOV-NO-PRINT', triaged: 14, confirmed_real: 7, false_positives: 7, false_positive_rate: 0.5, acceptance_rate: 0.5, total: 14, on_probation: true },
    { rule_id: 'SEC-SQLI-001', triaged: 22, confirmed_real: 20, false_positives: 2, false_positive_rate: 0.091, acceptance_rate: 0.909, total: 23, on_probation: false },
    { rule_id: 'SEC-SECRET-002', triaged: 31, confirmed_real: 29, false_positives: 2, false_positive_rate: 0.065, acceptance_rate: 0.935, total: 33, on_probation: false },
    { rule_id: 'AI-UNAWAITED-AUTH', triaged: 11, confirmed_real: 11, false_positives: 0, false_positive_rate: 0, acceptance_rate: 1, total: 11, on_probation: false },
  ] } }));
  await open(page, 'light', '#/calibration');
  await page.getByLabel('Repository').fill('acme/payments-api');
  await page.getByRole('button', { name: /Load metrics/ }).click();
  await expect(page.getByText('88.5%')).toBeVisible();
  await shot(page, '07_calibration.png');
});

test('labeling workbench', async ({ page }) => {
  const first = devCases[1];
  const status = { total: devCases.length, labeled: 0, adjudicated: 0, needs_adjudication: 0, unreviewed: devCases.length };
  await page.route('**/api/eval/cases', r => r.fulfill({ json: {
    split: 'development', cohorts: ['clean', 'hard_negative', 'defect'], reviewers: { a: null, b: null },
    progress: { reviewer_a: status, reviewer_b: status, adjudication: status }, predictions_available: true,
    cases: devCases.map((c: { id: string }) => ({ id: c.id, predicted_findings: 0, reviewer_a: 'UNREVIEWED', reviewer_b: 'UNREVIEWED', adjudication: 'UNREVIEWED' })) } }));
  await page.route('**/api/eval/score', r => r.fulfill({ json: { ready: false, progress: status,
    agreement: { cases_compared: 0, cohort_agreement: null, cohort_kappa: null, finding_agreement: null } } }));
  await page.route(`**/api/eval/cases/${first.id}?**`, r => r.fulfill({ json: {
    id: first.id, diff: readFileSync(join(corpus, first.file), 'utf8'), predicted_findings: [],
    label: { status: 'UNREVIEWED', cohort: null, rationale: '', findings: [] } } }));
  await open(page, 'dark', '#/labeling');
  await page.getByRole('button', { name: new RegExp(first.id) }).click();
  await expect(page.getByText('Cohort', { exact: true })).toBeVisible();
  await page.mouse.move(1400, 880);
  await page.evaluate(() => window.scrollTo(0, 380));
  await page.waitForTimeout(400);
  await shot(page, '08_labeling.png');
});
