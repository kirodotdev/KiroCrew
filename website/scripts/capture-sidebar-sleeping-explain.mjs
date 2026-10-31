/**
 * Screenshots of the "Show 1 sleeping session" expander under a resting
 * pointer, from capture/sidebar-sleeping-explain.html. Each frame ASSERTS what
 * it documents before it is kept:
 *
 *   before  the expander carries only a native `title` ("Not used in over
 *           7d..."), which a headless browser never paints, and no tooltip
 *           bubble opens on hover
 *   after   no native `title`; an instant tooltip opens on hover and says what
 *           sleeping means and which menu row changes it
 *
 * Usage (from website/; starts its own vite on 127.0.0.1:5199):
 *   node scripts/capture-sidebar-sleeping-explain.mjs <before|after> [outDir]
 * Shoot `before` with main's sidebar on disk, `after` with the fix.
 */
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const LABEL = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/sidebar-sleeping-explain'
if (!['before', 'after'].includes(LABEL)) {
  console.error(`usage: … <before|after> [outDir]  (got "${LABEL}")`)
  process.exit(2)
}
const EXPLAINS = 'Sleeping sessions are still open'

mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const BASE = 'http://127.0.0.1:5199'

const browser = await chromium.launch()
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 640, height: 420 }, deviceScaleFactor: 2, colorScheme: theme })
  page.on('pageerror', e => { console.error('pageerror:', e.message); failed = true })
  await page.goto(`${BASE}/capture/sidebar-sleeping-explain.html?theme=${theme}`)
  const expander = page.locator('[data-testid="stale-expander-f-demos"]')
  await expander.waitFor({ timeout: 60000 })
  await page.waitForTimeout(400)
  await expander.hover({ timeout: 5000 })
  // Longer than the instant tip's open delay, and past a native title's ~1s.
  await page.waitForTimeout(1500)

  const title = await expander.getAttribute('title')
  const tip = page.locator('[role="tooltip"]')
  const tipCount = await tip.count()
  const tipText = tipCount ? await tip.first().innerText() : ''
  const ok = LABEL === 'before'
    ? !!title && title.startsWith('Not used in over') && tipCount === 0
    : title === null && tipCount === 1 && tipText.includes(EXPLAINS)
  console.log(`${theme}-${LABEL}: ${ok ? 'OK' : 'MISMATCH'} title=${JSON.stringify(title)} tips=${tipCount} tip=${JSON.stringify(tipText)}`)
  if (!ok) failed = true
  await page.screenshot({ path: `${OUT}/${theme}-${LABEL}.png` })
  await page.close()
}

await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
