/**
 * Screenshot harness for the stale-collapse expander's counted sentence.
 *
 * Reproduces the gui-user-test friction shape exactly: one expanded "Demos"
 * folder whose header badge says 2, one fresh session visible inside it, and
 * the second session dormant — so the row under it must account for the
 * missing one ("1 dormant session hidden") instead of reading as a category.
 *
 * Serves the REAL built SPA (website/dist) with /api/** stubbed. Captures the
 * sidebar in the collapsed state (light + dark), the tooltip-bearing hover,
 * and the expanded state where the same row flips to "1 dormant session shown".
 * Every shot asserts that no dialog is open first: first-run gates render on
 * top of every frame while DOM assertions still pass behind them.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-stale-collapse-hidden-count.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || (process.env.KIROCREW_SCRATCH || '/tmp') + '/stale-collapse-hidden-count'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const hoursAgo = h => new Date(NOW - h * 3600_000).toISOString()

const folders = [
  { id: 'demos', name: 'Demos', order: 1, collapsed: false },
]

const slot = (key, title, folder_id, ageHours, extra = {}) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: hoursAgo(ageHours + 2), last_turn_ts: hoursAgo(ageHours), folder_id, ...extra,
})

const slots = [
  slot('s-d1', 'Fix empty-glob crash in list_sessions', 'demos', 3),
  slot('s-d2', 'Sidebar folder drag & drop', 'demos', 9 * 24),
  slot('s-r1', 'Release notes for 0.9', '', 1),
]

/** A first-run gate (Privacy, Customize) sits on top of every frame while DOM
 *  assertions still pass behind it — refuse to shoot through one. */
async function assertNoDialog(page, label) {
  const dialogs = await page.locator('[role="dialog"]:visible').count()
  if (dialogs > 0) throw new Error(`${label}: ${dialogs} unexpected dialog(s) open`)
}

/** Clip to the sidebar column around the folder row: the row's own x extent,
 *  widened by a margin so the header badge and the expander both sit inside. */
async function shoot(page, header, name) {
  await assertNoDialog(page, name)
  const box = await header.boundingBox()
  if (!box) throw new Error(`${name}: folder row has no box`)
  const x = Math.max(0, box.x - 12)
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x, y: Math.max(0, box.y - 60), width: Math.min(1280 - x, box.width + 24), height: 240 },
  })
  console.log('wrote', name)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      await stubDashboardApi(page, {
        folders, slots, theme,
        localStorageEntries: { 'mc-privacy-notice-v1': '1' },
      })
      logPageProblems(page)
      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

      const expander = page.getByTestId('stale-expander-demos')
      await expander.waitFor({ state: 'visible', timeout: 15_000 })
      // Folder badge 2 = 1 visible row + the sentence's 1.
      const header = page.locator('[data-folder-row="demos"]')
      const headerText = await header.innerText()
      if (!/\b2\b/.test(headerText)) throw new Error(`folder header does not show 2: ${JSON.stringify(headerText)}`)
      const label = (await expander.innerText()).trim()
      if (!label.startsWith('1 dormant session hidden')) throw new Error(`unexpected expander text: ${JSON.stringify(label)}`)
      const title = await expander.getAttribute('title')
      if (title !== 'Not used in over 7d. Click to show.') throw new Error(`unexpected title: ${JSON.stringify(title)}`)

      await shoot(page, header, `01-${theme}-collapsed`)

      if (theme === 'dark') {
        await expander.hover()
        await page.waitForTimeout(1200) // native title tooltips are not painted by headless Chromium; the hover state itself is.
        await shoot(page, header, `02-${theme}-hover`)

        await expander.click()
        await page.getByText('Sidebar folder drag & drop').waitFor({ state: 'visible', timeout: 5_000 })
        const shown = (await expander.innerText()).trim()
        if (!shown.startsWith('1 dormant session shown')) throw new Error(`unexpected expanded text: ${JSON.stringify(shown)}`)
        await shoot(page, header, `03-${theme}-expanded`)
      }
      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
