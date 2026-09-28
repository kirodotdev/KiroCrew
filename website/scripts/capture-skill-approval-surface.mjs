/**
 * Screenshot harness for #3927: surfacing the auto-approve setting in the
 * pending skill review panel and the staged-candidate notification.
 *
 * Same pattern as capture-skill-notification.mjs: the REAL built SPA behind an
 * in-process static server, every /api/** answered from fixtures.
 *
 * Frames:
 *   01-notification-actions  bell feed detail: the new "Auto-approve future
 *                            skills" action beside "Review skill"
 *   02-pending-panel         review queue: reworded hint (opt-out + script
 *                            caveat) with the settings link, and the script
 *                            badge (title-bearing) on the script row
 *   03-script-note-expanded  the script row expanded, showing the
 *                            always-requires-review note above the content
 *
 * Usage: node scripts/capture-skill-approval-surface.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skill-approval-surface'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const REVIEW_URL = '/capabilities?tab=skills&review=summarize-oncall-handoffs'
const SETTING_URL = '/settings?tab=skills&highlight=key:skills.approval_required'

// The note exactly as the changed backend now emits it: review deep-link plus
// the auto-approve settings shortcut.
const SKILL_NOTE = {
  kind: 'skills',
  source: 'system',
  channel: 'system.skills',
  priority: 'default',
  title: 'New skill awaiting review',
  body: [
    '**auto/summarize-oncall-handoffs** — Digest the week\'s pages into a handoff brief',
    '\nGenerated from a session. Needs your approval before it can be used.',
    '\n**Triggers:** oncall handoff, weekly summary',
  ].join('\n'),
  ts: '2026-08-16T07:20:31.483437+00:00',
  url: REVIEW_URL,
  actions: [
    { id: 'review-skill', label: 'Review skill', url: REVIEW_URL },
    { id: 'auto-approve-skills', label: 'Stop requiring skill approval…', url: SETTING_URL },
  ],
  slug: 'summarize-oncall-handoffs',
  candidate_kind: 'new',
  target: '',
  acked: false,
}

const PENDING = [
  {
    slug: 'summarize-oncall-handoffs',
    name: 'auto/summarize-oncall-handoffs',
    description: 'Digest the week\'s pages into a handoff brief',
    has_scripts: false,
    kind: 'new',
    target: null,
    base_version: null,
  },
  {
    slug: 'rotate-staging-fixtures',
    name: 'auto/rotate-staging-fixtures',
    description: 'Regenerate staging fixtures from the latest schema',
    has_scripts: true,
    kind: 'new',
    target: null,
    base_version: null,
  },
]

const apiFor = pending => async (path, route) => {
  if (path === '/api/notifications') {
    await json(route, { notifications: [SKILL_NOTE], unread: 1 })
    return true
  }
  if (path === '/api/skills/-/pending') {
    await json(route, { pending })
    return true
  }
  if (path.startsWith('/api/skills/-/pending/')) {
    await json(route, {
      name: 'auto/rotate-staging-fixtures',
      content: '---\nname: rotate-staging-fixtures\n---\n\n## Steps\n\n1. Regenerate the fixtures.\n',
      scripts: [{ filename: 'rotate.sh', content: '# regenerates fixtures\n' }],
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
  // ── Frame 01: notification detail with both actions ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra: apiFor(PENDING) })
    await page.goto(`${base}/notifications`, { waitUntil: 'networkidle' })
    await page.getByText('New skill awaiting review').first().click()
    await page.getByText('Stop requiring skill approval').first().waitFor()
    await shot(page, '01-notification-actions')
    await page.close()
  }

  // ── Frames 02 + 03: the pending panel hint + expanded script note ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra: apiFor(PENDING) })
    await page.goto(`${base}/capabilities?tab=skills`, { waitUntil: 'networkidle' })
    await page.getByText('can go live automatically').first().waitFor()
    await page.getByText('rotate-staging-fixtures').first().waitFor()
    await shot(page, '02-pending-panel')

    // Expand the script-bearing row: the always-requires-review note renders
    // as visible text above the candidate content.
    await page.getByRole('button', { name: 'Review', exact: true }).nth(1).click()
    await page.getByText('Bundled scripts always require manual review').first().waitFor()
    await shot(page, '03-script-note-expanded')
    await page.close()
  }
  console.log(`wrote frames to ${OUT} (prefix ${PREFIX})`)
} finally {
  await browser.close()
  srv.close()
}
