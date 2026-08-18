/**
 * Screenshot + measurement harness for the side panel's dock flip.
 *
 * The bug this proves: the dock wrapper keeps ONE React key across a flip, and
 * framer-motion does not release a key that leaves `animate` — it keeps owning
 * the inline style and drives the value back to its base. With only the
 * travelling axis targeted, right -> bottom -> right came back with
 * `height: 0px` inline, which outranks the wrapper's `h-full` class.
 *
 * So the evidence is the THIRD frame, not the first: a right-docked panel after
 * a round trip through the bottom dock. Each frame also logs the wrapper's
 * inline style and measured box, because "collapsed" is a number, not a vibe.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free). Only the
 * network and the localStorage seed are stubbed; the client code is unmodified.
 *
 * Usage: node scripts/capture-side-panel-dock-flip.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/side-panel-dock-flip'
const SLOT = 'side-panel-dock-flip'
const VIEWPORT = { width: 1400, height: 900 }

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Side panel dock flip',
  running: false,
  messages: 2,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-18T21:00:00Z',
  folder_id: '',
}]

/** Read the portal container, the motion wrapper, and the panel root together.
 *  The wrapper is `#activity-bar-slot > div` — ChatPage portals the motion.div
 *  into the App shell's actbar grid area, and the panel root is its only child. */
async function measure(page) {
  return page.evaluate(() => {
    const host = document.getElementById('activity-bar-slot')
    const wrap = host?.firstElementChild
    const panel = wrap?.firstElementChild
    const box = el => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top) }
    }
    return {
      host: box(host),
      wrapper: box(wrap),
      panel: box(panel),
      wrapperInline: wrap ? { width: wrap.style.width, height: wrap.style.height } : null,
      wrapperClass: wrap ? wrap.className : null,
      // What the class asks for vs what actually won.
      wrapperComputedH: wrap ? getComputedStyle(wrap).height : null,
    }
  })
}

async function shoot(page, name, label) {
  const m = await measure(page)
  console.log(`\n### ${label}`)
  console.log(JSON.stringify(m, null, 1))
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  return m
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, { slots, theme: 'dark' })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      tabs: [
        { id: 'changes', kind: 'changes', title: 'Changes' },
        { id: 'files', kind: 'files', title: 'Files' },
        { id: 'artifacts', kind: 'artifacts', title: 'Artifacts' },
      ],
      activeId: 'files',
    }))
    // Start from the default dock so frame 1 is the untouched right column.
    localStorage.removeItem('mc-side-panel-dock')
  }, SLOT)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const toBottom = page.locator('button[aria-label="Dock panel below chat"]')
  await toBottom.first().waitFor({ state: 'visible', timeout: 15000 })

  const a = await shoot(page, '01-right-initial', 'RIGHT dock, never flipped (baseline)')

  await toBottom.first().click()
  await page.waitForTimeout(900)
  const b = await shoot(page, '02-bottom', 'BOTTOM dock, after one flip')

  const toRight = page.locator('button[aria-label="Dock panel to the right"]')
  await toRight.first().waitFor({ state: 'visible', timeout: 10000 })
  await toRight.first().click()
  await page.waitForTimeout(900)
  const c = await shoot(page, '03-right-after-flip', 'RIGHT dock again, after the round trip')

  // The verdict: frame 3 must match frame 1's height. Printed rather than
  // asserted so the same script documents the bug and the fix.
  console.log('\n=== VERDICT ===')
  console.log('panel height  right-initial :', a.panel?.h)
  console.log('panel height  right-after   :', c.panel?.h)
  console.log('wrapper inline height after :', JSON.stringify(c.wrapperInline?.height))
  console.log('bottom-dock panel height    :', b.panel?.h)
  const same = a.panel?.h === c.panel?.h
  console.log(same
    ? 'OK — the round trip restored the panel height'
    : `REGRESSION — height changed ${a.panel?.h} -> ${c.panel?.h} after the round trip`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
