/**
 * Screenshot + overlap-guard harness for the Notes panel at 390px (#10254).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Scene: the Notes app on a phone-width viewport with the tree panel open and
 * no note active. The note column squeezes to zero width; its
 * absolutely-positioned header controls (sync pill, view switch) used to
 * escape the zero box and land on the panel's tree rows beneath. The fix
 * removes the squeezed column from layout while keeping it mounted.
 *
 * The harness FAILS when any foreign control intersects the first tree row,
 * so a layout regression cannot produce a plausible-looking screenshot of a
 * broken frame.
 *
 * Captures:
 *   after.png   panel at 390px, first row fully visible
 *
 * Usage: node scripts/capture-mdnb-panel-overlap.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc, mdnbNotesList } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-panel-overlap'
const NOTE_PATH = 'welcome.md'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  locale: 'en-US',
  isMobile: true,
  hasTouch: true,
})
const page = await context.newPage()
await stubDashboardApi(page, {
  theme: 'dark',
  extra: mdnbApiStub({
    notes: mdnbNotesList(NOTE_PATH, 'Welcome'),
    doc: mdnbNoteDoc(NOTE_PATH, '# Welcome\n\nHello.\n'),
  }),
})
logPageProblems(page)
await page.addInitScript(vaultId => {
  localStorage.setItem('mdnb-active-vault', vaultId)
}, MDNB_VAULT_ID)
await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
await page.getByText('Welcome').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(800)

// The first tree row must own its rect: no foreign control may intersect it.
// (Each row's own hover actions are positioned by the row itself and are out
// of scope here; the sync pill and view switcher belong to the note column.)
const firstRow = page.getByText('Meeting Notes').first()
await firstRow.waitFor({ timeout: 15000 })
const rowBox = await firstRow.boundingBox()
const intruders = await page.evaluate(rect => {
  const hits = []
  for (const el of document.querySelectorAll('button')) {
    const text = (el.textContent || '').trim()
    if (!text || /^(Pin|Duplicate|Rename|Delete)/.test(text)) continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    const ix = Math.min(r.x + r.width, rect.x + rect.width) - Math.max(r.x, rect.x)
    const iy = Math.min(r.y + r.height, rect.y + rect.height) - Math.max(r.y, rect.y)
    if (ix > 4 && iy > 4) hits.push(`${text.slice(0, 30)}@${Math.round(r.x)},${Math.round(r.y)}`)
  }
  return hits
}, { x: rowBox.x, y: rowBox.y, width: rowBox.width, height: rowBox.height })
if (intruders.length) {
  throw new Error(`foreign controls overlap the first tree row: ${intruders.join('; ')}`)
}
await page.screenshot({ path: `${OUT}/after.png` })
console.log('wrote', `${OUT}/after.png`, '(no intruders)')
await browser.close()
srv.close()
