/**
 * Screenshot harness for the Older-sessions pane's open-tab exclusion.
 *
 * The pane lists the sessions that are NOT open as a tab. The only thing that
 * decides what it renders is what `GET /api/sessions` returns, so BEFORE and
 * AFTER are produced by the SAME harness with the same fixture and the same
 * component — the one variable is whether the stubbed endpoint honours
 * `exclude_open=1`. That is exactly the change under review, and it keeps the two
 * sides of the pair from drifting the way two separate harnesses would.
 *
 * Three shots:
 *   00-BEFORE  endpoint ignores the flag -> every open tab is listed a second
 *              time, the active conversation at the top (it is the newest mtime)
 *   01-AFTER   endpoint honours it -> only the genuinely closed sessions remain
 *   02-AFTER   nothing left to list -> the new empty-state line, a state the old
 *              behaviour could not reach because disk sessions guaranteed rows
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures (gateway-free).
 *
 * Usage: node scripts/capture-older-sessions-dedupe.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/older-sessions-dedupe'
const ACTIVE = 'chat-7-1755400000'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

/** Three tabs the user has open. The sidebar lists these above the pane. */
const slots = [
  {
    key: ACTIVE, title: '侧边栏「较早的会话」重复问题', running: false, messages: 24,
    agent: 'kirocrew', modified: now, last_ts: '2026-08-17T22:05:00Z', folder_id: '',
    last_message: 'The pane is the complement of the tab list.',
  },
  {
    key: 'chat-6-1755300000', title: 'App Store builtin rows from catalog', running: false,
    messages: 61, agent: 'kirocrew', modified: now - 3600, last_ts: '2026-08-17T21:05:00Z',
    folder_id: '', last_message: 'PR #4200 is review-ready.',
  },
  {
    key: 'chat-5-1755200000', title: 'Advisory review lanes need teeth', running: false,
    messages: 18, agent: 'kirocrew', modified: now - 7200, last_ts: '2026-08-17T20:05:00Z',
    folder_id: '', last_message: 'Four prompt edits plus ratchets.',
  },
]

/** The transcript stem each open tab writes to — what list_sessions reports. */
const openStems = slots.map(s => `dashboard_${s.key}`)

/** Sessions on disk that no live slot holds: the pane's real content. */
const closedSessions = [
  { key: 'dashboard_chat-4-1755100000', title: 'Credit pill error state', agent: 'kirocrew', modified: now - 86400 },
  { key: 'dashboard_chat-3-1755000000', title: 'Thinking bursts per segment', agent: 'kirocrew', modified: now - 172800 },
  { key: 'slack_1712793600.123456', title: 'Slack thread — release 0.1.4', agent: 'kirocrew', modified: now - 259200 },
]

/** Every session file on disk, newest first — what the endpoint used to return. */
const allSessions = [
  ...slots.map(s => ({
    key: `dashboard_${s.key}`, title: s.title, agent: s.agent, modified: s.modified,
  })),
  ...closedSessions,
].sort((a, b) => b.modified - a.modified)

/**
 * @param {'ignore'|'honour'} mode  whether the stub implements exclude_open
 * @param {boolean} everythingOpen  serve a disk that holds only open sessions
 */
function sessionsRoute(mode, everythingOpen) {
  return async (path, route) => {
    if (path !== '/api/sessions') return false
    const url = new URL(route.request().url())
    const wants = ['1', 'true', 'yes'].includes((url.searchParams.get('exclude_open') || '').toLowerCase())
    const disk = everythingOpen ? allSessions.filter(s => openStems.includes(s.key)) : allSessions
    const rows = mode === 'honour' && wants ? disk.filter(s => !openStems.includes(s.key)) : disk
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ sessions: rows, total: rows.length, has_more: false }),
    })
    return true
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 980 }, deviceScaleFactor: 2 })

  const shots = [
    { name: '00-BEFORE-older-sessions-duplicated', mode: 'ignore', everythingOpen: false },
    { name: '01-AFTER-older-sessions-complement', mode: 'honour', everythingOpen: false },
    { name: '02-AFTER-older-sessions-empty', mode: 'honour', everythingOpen: true },
  ]

  for (const shot of shots) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme: 'dark', extra: sessionsRoute(shot.mode, shot.everythingOpen) })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)

    // The pane is collapsed by default; opening it is what fetches the page.
    await page.getByRole('button', { name: /^older sessions$/i }).click()
    await page.locator('#history-pane').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(1200)

    const rows = await page.locator('#history-pane [role="button"]').count()
    const empty = await page.locator('#history-pane').innerText()
    console.log(`${shot.name}: ${rows} row(s) rendered${/no older sessions/i.test(empty) ? ' + empty-state line' : ''}`)

    const side = await page.locator('#history-pane').evaluate(el => {
      const rail = el.closest('aside') || el.parentElement
      const r = rail.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    })
    await page.screenshot({
      path: `${OUT}/${shot.name}.png`,
      clip: {
        x: Math.max(0, side.x),
        y: Math.max(0, side.y),
        width: Math.min(side.width, 360),
        height: Math.min(side.height, 900),
      },
    })
    await page.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
