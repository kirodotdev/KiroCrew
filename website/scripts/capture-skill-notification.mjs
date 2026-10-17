/**
 * Screenshot harness for the actionable pending-skill notification.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * What the change is about, and therefore what each frame has to prove:
 * a staged skill candidate is invisible until a human approves it, so the note
 * is the only surface announcing it — yet it shipped with no deep link, no
 * action button, a generic bell icon, and a body that only restated the title.
 *
 * Frames:
 *   01-feed-row        bell feed: Skills icon + label, detail-bearing body,
 *                      and the "Review skill" action capsule
 *   02-detail-panel    detail view: the Open button and the rendered body
 *                      (What it does / Triggers / script warning)
 *   03-review-deeplink /capabilities?tab=skills&review=<slug> — the linked
 *                      candidate expanded, scrolled to and ring-highlighted,
 *                      while its sibling stays collapsed
 *   04-review-resolved same link when the candidate is already approved or
 *                      dismissed: says so instead of a silently normal tab
 *
 * Usage: node scripts/capture-skill-notification.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skill-notification'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const REVIEW_URL = '/capabilities?tab=skills&review=resolve-i18n-catalog-rebase-conflicts'

// The note as the fixed backend now emits it: system.skills channel, a body
// carrying the candidate's own description + triggers, a validated note-level
// `url`, and one navigation action.
const SKILL_NOTE = {
  kind: 'skills',
  source: 'system',
  channel: 'system.skills',
  priority: 'default',
  title: 'New skill awaiting review',
  body: [
    '**auto/resolve-i18n-catalog-rebase-conflicts** — Recover branch-added i18n keys '
      + 'after a rebase drops them from the catalogs',
    '\nGenerated from a session. Needs your approval before it can be used.',
    '\n**Triggers:** i18n rebase conflict, dead key ratchet, locale drift',
    '\n_Bundles executable scripts — review them before approving._',
  ].join('\n'),
  ts: '2026-08-04T07:20:31.483437+00:00',
  url: REVIEW_URL,
  actions: [{ id: 'review-skill', label: 'Review skill', url: REVIEW_URL }],
  slug: 'resolve-i18n-catalog-rebase-conflicts',
  candidate_kind: 'new',
  target: '',
  acked: false,
}

// A second, ordinary note so the frame shows the Skills treatment beside a
// neighbour rather than in isolation.
const CRON_NOTE = {
  kind: 'cron',
  source: 'system',
  channel: 'system.cron',
  priority: 'default',
  title: 'Nightly registry sweep',
  body: 'Checked 41 entries, nothing to do.',
  ts: '2026-08-04T06:05:00.000000+00:00',
  acked: true,
}

const PENDING_LINKED = {
  slug: 'resolve-i18n-catalog-rebase-conflicts',
  name: 'auto/resolve-i18n-catalog-rebase-conflicts',
  description: 'Recover branch-added i18n keys after a rebase drops them from the catalogs',
  has_scripts: true,
  kind: 'new',
  target: null,
  base_version: null,
}
const PENDING_SIBLING = {
  slug: 'github-pr-push-event-not-firing',
  name: 'auto/github-pr-push-event-not-firing',
  description: 'Force a fresh push event when a workflow-file diff suppresses the webhook',
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
}

const DETAIL = {
  name: 'auto/resolve-i18n-catalog-rebase-conflicts',
  content: [
    '---',
    'name: resolve-i18n-catalog-rebase-conflicts',
    'source: auto',
    '---',
    '',
    '## Steps',
    '',
    '1. Diff the catalogs against the merge base, not the branch tip.',
    '2. Re-apply every key the branch added that the rebase dropped.',
    '3. Regenerate en-XA.json — it is generated, never hand-edited.',
  ].join('\n'),
  scripts: [{ filename: 'recover-keys.mjs', content: "// walks locales/ and re-inserts dropped keys\n" }],
}

/** Route /api/** for a given pending-queue fixture.
 *
 *  MUST return truthy once it has fulfilled — `stubDashboardApi` reads the
 *  return value to decide whether to fall through to its own defaults, and
 *  `json()` resolves to undefined, so returning it directly makes the base stub
 *  fulfill the same route a second time ("Route is already handled!"). */
const apiFor = pending => async (path, route) => {
  if (path === '/api/notifications') {
    await json(route, { notifications: [CRON_NOTE, SKILL_NOTE], unread: 1 })
    return true
  }
  if (path === '/api/skills/-/pending') {
    await json(route, { pending })
    return true
  }
  if (path.startsWith('/api/skills/-/pending/')) {
    await json(route, DETAIL)
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
  // ── Frames 01 + 02: the bell feed and the detail panel ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra: apiFor([PENDING_LINKED, PENDING_SIBLING]) })
    await page.goto(`${base}/notifications`, { waitUntil: 'networkidle' })
    await page.getByText('New skill awaiting review').first().waitFor()
    await shot(page, '01-feed-row')

    await page.getByText('New skill awaiting review').first().click()
    await page.getByRole('button', { name: 'Open' }).first().waitFor()
    await shot(page, '02-detail-panel')
    await page.close()
  }

  // ── Frame 03: the candidate-level deep link ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra: apiFor([PENDING_SIBLING, PENDING_LINKED]) })
    await page.goto(`${base}${REVIEW_URL}`, { waitUntil: 'networkidle' })
    // The linked row auto-expands; its SKILL.md body is the proof.
    await page.getByText('## Steps').first().waitFor()
    await shot(page, '03-review-deeplink')
    await page.close()
  }

  // ── Frame 04: the same link once the candidate is gone ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 620 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra: apiFor([]) })
    await page.goto(`${base}${REVIEW_URL}`, { waitUntil: 'networkidle' })
    await page.getByText(/no longer awaiting review/i).first().waitFor()
    await shot(page, '04-review-resolved')
    await page.close()
  }
  console.log(`wrote frames to ${OUT} (prefix ${PREFIX})`)
} finally {
  await browser.close()
  srv.close()
}
