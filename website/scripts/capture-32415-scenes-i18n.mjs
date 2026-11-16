/**
 * Screenshot harness for the Agent Worlds in-scene text (#32415): the Panda Den
 * whiteboard headers and the hover card's status line, in the active language.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with the
 * shared /api stub; four running sessions seat four pandas so the board has cards
 * and a desk has someone to hover.
 *
 * Usage: node scripts/capture-32415-scenes-i18n.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2]
if (!OUT) throw new Error('usage: node scripts/capture-32415-scenes-i18n.mjs <outDir>')
mkdirSync(OUT, { recursive: true })

const now = new Date().toISOString()
const slots = ['Rosalinda', 'Ferdinand', 'Bernadett', 'Constanza'].map((title, i) => ({
  key: `chat-${i}`, title, running: true, messages: 3, agent: 'kirocrew', modified: now, last_ts: now,
  folder_id: '', last_message: '', source_links: [], source_links_total: 0, tags: [],
}))

const extra = (path, route) => {
  if (path === '/api/crons') return json(route, { jobs: [] }), true
  if (path === '/api/spawn/list' || path.startsWith('/api/spawn')) return json(route, { runs: [] }), true
  return false
}

/** Scene canvas is 440x300 logical pixels; a seated panda sits at desk (40,120) + (10,20). */
const SEAT = { x: 50, y: 140 }

async function shoot(browser, base, { lang, theme, expectHeader, expectTooltip, file }) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 2, locale: lang })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, theme, extra,
    localStorageEntries: { 'mc-lang': lang, 'mc-agent-scene': 'panda', 'mc-privacy-notice-v1': '1' },
  })
  await page.goto(base + '/worlds', { waitUntil: 'domcontentloaded' })
  // SCENES order puts Panda Den second in the picker; the picker's name is localized.
  await page.getByRole('radio').nth(1).click({ timeout: 15_000 })
  const canvas = page.locator('canvas:visible').first()
  await canvas.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(9000)
  const box = await canvas.boundingBox()
  const k = box.width / 440
  await page.mouse.move(box.x + SEAT.x * k, box.y + SEAT.y * k)
  const tip = page.getByText(expectTooltip, { exact: true })
  await tip.waitFor({ state: 'visible', timeout: 5000 })
  // The board is canvas text: assert it through the overlay canvas' recorded draw calls is
  // not possible from outside, so the hover card is the DOM assertion and the board is read
  // from the PNG by the reviewer.
  await page.screenshot({ path: join(OUT, file) })
  console.log(`wrote ${file} (asserted hover card "${expectTooltip}"; board header "${expectHeader}" in the PNG)`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, { lang: 'en', theme: 'dark', expectHeader: 'To Do', expectTooltip: 'Grinding PRs', file: '01-panda-en.png' })
    await shoot(browser, base, { lang: 'zh-CN', theme: 'dark', expectHeader: '待办', expectTooltip: '肝 PR 中', file: '02-panda-zh-CN.png' })
    await shoot(browser, base, { lang: 'de', theme: 'light', expectHeader: 'Zu erledigen', expectTooltip: 'Schuftet an PRs', file: '03-panda-de-light.png' })
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
