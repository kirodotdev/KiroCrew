/**
 * Screenshot harness for #10861: the pending-skill approval surface explains
 * WHY an approve cannot succeed (pre-click badge + warning) and why a click
 * was refused (coded 422 with the validator's findings).
 *
 * Same pattern as capture-skill-approval-surface.mjs: the REAL built SPA
 * behind an in-process static server, every /api/** answered from fixtures.
 *
 * Frames:
 *   01-pending-badge        review queue: the "fails validation" badge on a
 *                           flagged candidate's card, next to the script badge
 *   02-warning-expanded     the flagged row expanded: the pre-approval warning
 *                           opened to show the per-file validator findings
 *   03-refusal-notice       after clicking Approve: the ErrorNotice with the
 *                           coded refusal reason and the same findings
 *
 * Usage: node scripts/capture-skill-approve-validation.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skill-approve-validation-10861'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const REPORT = { 'summarize.py': ["dynamic exec/import: eval()"] }

const PENDING = [
  {
    slug: 'evil-helper',
    name: 'auto/evil-helper',
    description: 'Summarize deploy logs quickly',
    has_scripts: true,
    kind: 'new',
    target: null,
    base_version: null,
    script_validation: { ok: false, report: REPORT },
  },
  {
    slug: 'rotate-staging-fixtures',
    name: 'auto/rotate-staging-fixtures',
    description: 'Regenerate staging fixtures from the latest schema',
    has_scripts: true,
    kind: 'new',
    target: null,
    base_version: null,
    script_validation: { ok: true, report: {} },
  },
]

const state = { queueEmptied: false }

const api = async (path, route) => {
  if (path === '/api/skills/-/pending') {
    // Stateful: after frame 05's not-found approve fires, the refetch reads
    // an EMPTY queue — the exact arrangement frame 05 exists to show.
    await json(route, { pending: state.queueEmptied ? [] : PENDING })
    return true
  }
  if (path === '/api/skills/-/pending/evil-helper/approve') {
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        error: 'script validation failed',
        code: 'script_validation_failed',
        report: REPORT,
      }),
    })
    return true
  }
  if (path === '/api/skills/-/pending/rotate-staging-fixtures/approve') {
    // Frame 05's fixture: the candidate was resolved elsewhere; the refusal
    // is not-found and the refetch drains the queue.
    state.queueEmptied = true
    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
    })
    return true
  }
  if (path === '/api/skills/-/pending/rotate-staging-fixtures/dismiss') {
    // Frame 04's fixture: the dismiss fails, and the panel must say so
    // instead of silently keeping the row (issue #10861's bug class, on the
    // Dismiss button). Uncoded 503: the coded not-found variant renders the
    // muted achieved-outcome treatment and is pinned by vitest instead.
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'gateway restarting' }),
    })
    return true
  }
  if (path.startsWith('/api/skills/-/pending/')) {
    await json(route, {
      name: 'auto/evil-helper',
      content: '---\nname: evil-helper\n---\n\n## Steps\n\n1. Run the bundled helper over the log file.\n',
      scripts: [{ filename: 'summarize.py', content: "import sys\nprint(eval(sys.argv[1]))\n" }],
      script_validation: { ok: false, report: REPORT },
    })
    return true
  }
  if (path === '/api/skills') {
    await json(route, [])
    return true
  }
  return false
}

const shot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
  logPageProblems(page)
  await stubDashboardApi(page, { extra: api })
  await page.goto(`${base}/capabilities?tab=skills`, { waitUntil: 'networkidle' })

  // ── Frame 01: the fails-validation badge on the card, pre-click ──
  await page.getByText('auto/evil-helper').first().waitFor()
  await page.getByText('fails validation').first().waitFor()
  await shot(page, '01-pending-badge')

  // ── Frame 02: expanded row, warning opened to the findings ──
  await page.getByRole('button', { name: 'Review', exact: true }).first().click()
  const warning = page.getByText('Validation findings', { exact: true }).first()
  await warning.waitFor()
  await warning.click() // open the <details>
  await page.getByText('dynamic exec/import: eval()').first().waitFor()
  await shot(page, '02-warning-expanded')

  // ── Frame 03: the refused click explains itself ──
  await page.getByRole('button', { name: 'Approve', exact: true }).first().click()
  await page
    .getByText('Approval refused: the bundled scripts failed validation — approving again without changes returns this same refusal. Fix the scripts, then approve.')
    .first()
    .waitFor()
  await shot(page, '03-refusal-notice')

  // ── Frame 04: a failed Dismiss explains itself at panel level ──
  page.once('dialog', d => d.accept()) // the Dismiss confirm()
  await page
    .getByRole('button', { name: 'Dismiss', exact: true })
    .nth(1) // the clean sibling row (rotate-staging-fixtures)
    .click()
  await page
    .getByText('Dismiss of “auto/rotate-staging-fixtures” failed (server error — retry in a moment).', { exact: true })
    .first()
    .waitFor()
  await shot(page, '04-dismiss-failure-notice')

  // ── Frame 05: a not-found approve refusal above an EMPTIED queue ──
  // The refetch removes the last row; the panel notice must not read as an
  // orphaned error above nothing (UX asked for a human-reviewable frame of
  // exactly this arrangement).
  await page.getByRole('button', { name: 'Review', exact: true }).first().click()
  const approve2 = page.getByRole('button', { name: 'Approve', exact: true }).nth(1)
  await approve2.click()
  // The refusal lead is shared by the hedging and the resolved sentences, so a
  // full-container exact-text match cannot pick the frame's state. Anchor on
  // the single role=alert banner carrying the lead first …
  await page
    .getByRole('alert')
    .filter({ hasText: 'Candidate “auto/rotate-staging-fixtures” is no longer pending' })
    .first()
    .waitFor()
  // The named NOTICE also contains the candidate's name, so wait on the row's
  // own controls: an emptied queue has no Review buttons left.
  await page.getByRole('button', { name: 'Review', exact: true }).first().waitFor({ state: 'detached' })
  // … then on the SETTLED sentence: the stub live list never holds the
  // candidate, so the message itself states the dismissal (no separate
  // resolution line under a hedge — the frame must show one statement).
  await page
    .getByText('Candidate “auto/rotate-staging-fixtures” is no longer pending — it was dismissed in another window or by your Kiro Crew agent and does not appear in the Skills list below, so there was nothing left to approve. A replacement candidate can be proposed again later.', { exact: true })
    .first()
    .waitFor()
  await shot(page, '05-notfound-empty-queue')
  await page.close()
  console.log(`wrote frames to ${OUT} (prefix ${PREFIX})`)
} finally {
  await browser.close()
  srv.close()
}
