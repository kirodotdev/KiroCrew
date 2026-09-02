/**
 * Measurement + screenshot harness for the sessions panel's "Older Sessions"
 * footer row.
 *
 * Two things are under test and both are geometric, so pixels — not opinions —
 * are the verdict:
 *   1. the row's top hairline must land on the SAME screen y as the nav rail's
 *      community row hairline (the one above "Star us · Report issue"), and the
 *      clock + label must sit optically centred in the band below it,
 *   2. the disclosure chevron must be the RIGHTMOST control, trailing the Clear
 *      button that only exists while the pane is expanded.
 *
 * Runs the REAL built SPA (website/dist) behind an in-process static server with
 * every /api/** call answered from fixtures, so no gateway and no kiro-cli are
 * needed.
 *
 * Usage: node scripts/capture-older-sessions-header.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/older-sessions-header'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const slots = ['Spec Builder visual assets redesign', 'Theme pack font routing', 'Session summary panel'].map((title, i) => ({
  key: `s${i + 1}`,
  title,
  messages: 4,
  running: false,
  agent: 'kirocrew',
  created: '2026-08-09T01:00:00Z',
  last_ts: new Date(Date.parse('2026-08-11T18:00:00Z') - i * 3600_000).toISOString(),
  folder_id: '',
}))

// History rows are what make the Clear button render, so the expanded frame
// cannot prove the chevron ordering without them.
const history = Array.from({ length: 6 }, (_, i) => ({
  key: `dashboard-h${i + 1}`,
  title: `Archived session ${i + 1}`,
  agent: 'kirocrew',
  created: '2026-08-01T10:00:00Z',
  modified: new Date(Date.parse('2026-08-08T10:00:00Z') - i * 86_400_000).toISOString(),
  messages: 3,
}))

const probeFn = () => {
  // .rail-community-links IS a div, so closest('div') would return the links
  // themselves; the hairline lives on their PARENT row.
  const railRow = document.querySelector('nav .rail-community-links')?.parentElement
  const nav = document.querySelector('nav')
  const sidebar = document.querySelector('.sidebar-inner')
  const older = document.querySelector('[aria-controls="history-pane"]')
  if (!railRow || !nav || !sidebar || !older) {
    return { error: `missing: ${[!railRow && 'railRow', !nav && 'nav', !sidebar && 'sidebar', !older && 'older'].filter(Boolean).join(',')}` }
  }
  const r = el => el.getBoundingClientRect()
  const divider = older.previousElementSibling
  const clock = older.querySelector('svg')
  const chevron = [...older.querySelectorAll('svg')].pop()
  const clear = older.querySelector('button')
  const round = n => Math.round(n * 100) / 100
  return {
    railHairlineY: round(r(railRow).top),
    railCardInnerBottom: round(r(nav).bottom - 1),
    railBottomOffset: round(r(nav).bottom - 1 - r(railRow).top),
    olderHairlineY: round(r(divider).top),
    sidebarInnerBottom: round(r(sidebar).bottom - 1),
    olderBottomOffset: round(r(sidebar).bottom - 1 - r(divider).top),
    bandCentreY: round((r(divider).top + (r(sidebar).bottom - 1)) / 2),
    clockCentreY: round((r(clock).top + r(clock).bottom) / 2),
    chevronCentreY: round((r(chevron).top + r(chevron).bottom) / 2),
    chevronRight: round(r(chevron).right),
    rowRight: round(r(older).right),
    clearRight: clear ? round(r(clear).right) : null,
    chevronLeft: round(r(chevron).left),
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      // Must return TRUTHY after fulfilling: the shared stub awaits this hook
      // and falls through to its own fulfill on a falsy result, which throws
      // "Route is already handled!" — and `json()` resolves to undefined.
      if (path === '/api/sessions') { await json(route, { sessions: history, has_more: false }); return true }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const collapsed = await page.evaluate(probeFn)
  console.log('COLLAPSED', JSON.stringify(collapsed, null, 2))
  await page.screenshot({ path: `${OUT}/${PREFIX}-collapsed.png`, clip: { x: 0, y: 520, width: 760, height: 380 } })

  await page.click('[aria-controls="history-pane"]')
  await page.waitForTimeout(700)
  const expanded = await page.evaluate(probeFn)
  console.log('EXPANDED', JSON.stringify(expanded, null, 2))
  await page.screenshot({ path: `${OUT}/${PREFIX}-expanded.png`, clip: { x: 0, y: 300, width: 760, height: 600 } })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
