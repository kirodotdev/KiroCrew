/**
 * Screenshot harness for the sidebar's in-flow "Show all older sessions" row.
 *
 * Serves the REAL built SPA (website/dist) with /api/** stubbed. Fixture: a
 * short ungrouped root list with one dormant session (so the dormant expander
 * renders above the row), plus a folder so flat view has something to flatten,
 * and two closed sessions served as Older Sessions history.
 *
 * Captures: the row closing the root lane (light + dark), the Older Sessions
 * pane opened by clicking it (row gone), and the row closing the flat lane.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-older-sessions-hint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || (process.env.KIROCREW_SCRATCH || '/tmp') + '/older-sessions-hint'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const hoursAgo = h => new Date(NOW - h * 3600_000).toISOString()

const folders = [{ id: 'kiro', name: 'Kiro', order: 1, collapsed: false }]

const slot = (key, title, folder_id, ageHours, extra = {}) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: hoursAgo(ageHours + 2), last_turn_ts: hoursAgo(ageHours), folder_id, ...extra,
})

const slots = [
  slot('s-k1', 'Settings path navigation', 'kiro', 1),
  slot('s-r1', 'Fix sidebar drag bug', '', 2),
  slot('s-r2', 'KAS login UI', '', 20),
  slot('s-r3', 'Desktop update channels', '', 30 * 24),
]

// Closed sessions: what the Older Sessions pane lists once opened.
const history = [
  { key: 'h-1', title: 'Bedrock payments research', modified: (NOW - 3 * 86400_000) / 1000, agent: 'kirocrew', messages: 12 },
  { key: 'h-2', title: 'Theme pack debugging', modified: (NOW - 5 * 86400_000) / 1000, agent: 'kirocrew', messages: 7 },
]

// Nav rail + the sessions sidebar; the chat pane to the right is not the subject.
const CLIP = { x: 0, y: 0, width: 640, height: 900 }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  async function load({ theme, flat }) {
    const page = await context.newPage()
    await stubDashboardApi(page, {
      folders, slots, theme,
      localStorageEntries: flat ? { 'mc-sidebar-flat-view': '1' } : null,
      extra: async (path, route) => {
        if (path === '/api/sessions') {
          await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ sessions: history, has_more: false, total: history.length }) })
          return true
        }
        return false
      },
    })
    logPageProblems(page)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    return page
  }

  // 1. Root lane, light: the row follows the dormant expander.
  let page = await load({ theme: 'light', flat: false })
  console.log('root row (light):', await page.getByTestId('older-sessions-hint-root').count())
  await page.screenshot({ path: `${OUT}/01-root-lane-light.png`, clip: CLIP })
  await page.close()

  // 2. Root lane, dark; then click it — the pane opens and the row goes away.
  page = await load({ theme: 'dark', flat: false })
  await page.screenshot({ path: `${OUT}/02-root-lane-dark.png`, clip: CLIP })
  await page.getByTestId('older-sessions-hint-root').click()
  await page.waitForTimeout(800)
  console.log('root row after click:', await page.getByTestId('older-sessions-hint-root').count())
  await page.screenshot({ path: `${OUT}/03-pane-opened-dark.png`, clip: CLIP })
  await page.close()

  // 3. Flat lane, dark.
  page = await load({ theme: 'dark', flat: true })
  console.log('flat row:', await page.getByTestId('older-sessions-hint-flat').count())
  await page.screenshot({ path: `${OUT}/04-flat-lane-dark.png`, clip: CLIP })
  await page.close()

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
