/**
 * Evidence capture for the side panel's selected tab (#27241): renders the real
 * built SPA with a stubbed API, opens the right panel with enough document tabs
 * to overflow its strip, makes the LAST one active, and reports whether that
 * chip (and its close button) is fully inside the scroller, clear of the edge
 * fade. Also screenshots the strip.
 *
 * Usage: npm run build && node scripts/capture-side-panel-active-tab.mjs <outDir> [name]
 *   THEME=light   capture the light theme (default dark)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || (process.env.KIROCREW_SCRATCH || '/tmp') + '/side-panel-active-tab'
const NAME = process.argv[3] || 'shot'
const THEME = process.env.THEME === 'light' ? 'light' : 'dark'
const VIEWPORT = { width: 1280, height: 760 }
const SLOT = 'active-tab-demo'

const FILES = [
  'InstanceTabBar.tsx', 'index.css', 'SidePanel.tsx', 'sidePanelReveal.ts',
  'useScrollEdges.ts', 'ChatSidebar.tsx', 'CrewChatWindow.tsx', 'dashboardRefresh.ts',
]
const TABS = [
  { id: 'files', kind: 'files', title: 'Files' },
  ...FILES.map((name, i) => ({ id: 'f' + i, kind: 'file', title: name, path: '/repo/website/src/' + name })),
]
const ACTIVE = 'f' + (FILES.length - 1)

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: THEME,
    slots: [{
      key: SLOT, title: 'Selected tab demo', running: false, last_message: 'Checking the tab strip.',
      messages: 2, agent: 'kirocrew', memory_mode: 'persistent', folder_id: '',
      modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
    }],
    localStorageEntries: {
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({ activeId: ACTIVE, tabs: TABS }),
      ['mc-activity-open:' + SLOT]: 'true',
    },
  })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.side-panel-strip', { timeout: 20000 })
  await page.waitForSelector('[role="tab"][aria-selected="true"]', { timeout: 10000 })
  await page.waitForTimeout(1200)
  const m = await page.evaluate(() => {
    const chip = document.querySelector('[role="tab"][aria-selected="true"]')
    const scroller = chip?.closest('ul')
    if (!chip || !scroller) return null
    const c = chip.getBoundingClientRect()
    const s = scroller.getBoundingClientRect()
    const fade = document.querySelector('[data-testid="side-panel-tabs-cue-right"]')
    return {
      title: chip.textContent?.trim(),
      chipLeft: Math.round(c.left), chipRight: Math.round(c.right),
      scrollerLeft: Math.round(s.left), scrollerRight: Math.round(s.right),
      overflows: scroller.scrollWidth > scroller.clientWidth,
      scrollLeft: Math.round(scroller.scrollLeft),
      rightFade: !!fade,
      fadeLeft: fade ? Math.round(fade.getBoundingClientRect().left) : null,
    }
  })
  console.log('MEASURE ' + JSON.stringify(m))
  const strip = await page.$('.side-panel-strip')
  await strip.screenshot({ path: `${OUT}/${NAME}.png` })
  console.log(`saved ${OUT}/${NAME}.png`)
  await browser.close()
  srv.close()
}
main().catch(e => { console.error(e); process.exit(1) })
