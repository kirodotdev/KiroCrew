/**
 * Screenshot harness for "a session card shows at most three source-link chips
 * in total, pull requests first".
 *
 * The budget is applied server-side (`state_parts/source_links.py`'s `_budgeted_source_links`), so
 * the sidebar renders whatever the slots payload carries. The fixtures here are
 * the two shapes that function produces for the same sessions: the per-kind
 * slice (three pull requests AND three issues on one card) and the shared slice
 * (three chips, pull requests first, issues only in the slots left over). Each
 * row's `source_links_total` is the real link count, so the "+N" chip reads the
 * honest overflow in both shapes.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free). Only the
 * network and the localStorage seed are stubbed; the client code is unmodified.
 *
 * Usage: node scripts/capture-sidebar-chip-budget.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-chip-budget'
const ACTIVE = 'chat-a'
const REPO = 'https://github.com/kirodotdev/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const pr = (number, extra = {}) => ({ provider: 'github', number, url: `${REPO}/pull/${number}`, kind: 'change', ...extra })
const issue = number => ({ provider: 'github', number, url: `${REPO}/issues/${number}`, kind: 'issue' })

/** Newest mention first, as `_pr_source_links` orders them. */
const PRS = [pr(19258, { state: 'merged' }), pr(19274, { state: 'open', ci: 'passed' }), pr(19281, { state: 'open', ci: 'running' }), pr(19201, { state: 'closed' })]
const ISSUES = [issue(19594), issue(19581), issue(19587), issue(19503)]

const row = (key, title, links, total, minutesAgo, lastMessage) => ({
  key, title, running: false, messages: 12, agent: 'kirocrew',
  modified: now - minutesAgo * 60, last_ts: '2026-10-12T20:00:00Z', folder_id: '',
  last_message: lastMessage, source_links: links, source_links_total: total,
})

const active = {
  key: ACTIVE, title: 'Draft the release notes', running: false, messages: 4,
  agent: 'kirocrew', modified: now, last_ts: '2026-10-12T22:10:00Z', folder_id: '',
  last_message: 'Grouped the entries by area.',
}

/** The per-kind slice: 4 PRs + 4 issues -> 3 + 3 chips and "+2". */
const beforeSlots = [
  active,
  row('chat-b', 'Fix sidebar chip overflow', [...PRS.slice(0, 3), ...ISSUES.slice(0, 3)], 8, 120, 'Rebased and pushed; checks green.'),
]

/** The shared slice, changes first, for three different sessions. */
const afterSlots = [
  active,
  row('chat-b', 'Fix sidebar chip overflow', PRS.slice(0, 3), 8, 120, 'Rebased and pushed; checks green.'),
  row('chat-c', 'Members page a11y pass', [PRS[1], ...ISSUES.slice(0, 2)], 5, 1440, 'aria-current landed on the rail.'),
  row('chat-d', 'Triage: phone top bar follow-ups', ISSUES.slice(0, 3), 4, 1500, 'Filed the three follow-ups.'),
]

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

const rows = page => page.locator('.session-row').filter({ has: page.locator('a[title^="Open "]') })

async function cropRows(page, path) {
  await page.mouse.move(1500, 850)
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.waitForTimeout(250)
  const boxes = await rows(page).evaluateAll(els => els.map(el => {
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }
  }))
  const x = Math.min(...boxes.map(b => b.x))
  const y = Math.min(...boxes.map(b => b.y))
  const right = Math.max(...boxes.map(b => b.right))
  const bottom = Math.max(...boxes.map(b => b.bottom))
  await page.screenshot({
    path,
    clip: { x: Math.max(0, x - 8), y: Math.max(0, y - 8), width: right - x + 16, height: bottom - y + 16 },
  })
}

/** Chip labels per row, in DOM order, for the assertions below. */
const strips = page => rows(page).evaluateAll(els => els.map(el => ({
  chips: [...el.querySelectorAll('a[title^="Open "]')].map(a => a.textContent.trim()),
  overflow: el.querySelector('[data-testid="session-source-overflow"]')?.textContent.trim() ?? '',
})))

function expect(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    throw new Error(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`)
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  let page = null

  async function load(theme, slots) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await rows(page).first().getByTestId('session-source-overflow').waitFor({ state: 'visible', timeout: 15000 })
  }

  try {
    for (const theme of ['dark', 'light']) {
      await load(theme, beforeSlots)
      expect(`${theme} before`, await strips(page), [
        { chips: ['#19258', '#19274', '#19281', '#19594', '#19581', '#19587'], overflow: '+2' },
      ])
      await cropRows(page, `${OUT}/01-before-${theme}.png`)

      await load(theme, afterSlots)
      expect(`${theme} after`, await strips(page), [
        { chips: ['#19258', '#19274', '#19281'], overflow: '+5' },
        { chips: ['#19274', '#19594', '#19581'], overflow: '+2' },
        { chips: ['#19594', '#19581', '#19587'], overflow: '+1' },
      ])
      await cropRows(page, `${OUT}/02-after-${theme}.png`)
      console.log(`${theme}: before 6 chips (+2) -> after 3 chips per row`)
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
