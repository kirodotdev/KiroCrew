/** Real-browser evidence for #26939: a failed background re-read keeps the page.
 *
 * Drives website/capture/crew-dashboard-read-error.html, which mounts the REAL
 * `CrewDynamicDashboard`: the first read answers a page, every later read a 503.
 * Each shot asserts the branch before the screenshot: the SAME iframe element is
 * still mounted, the read-error band is up, and the old error card is absent.
 *
 *   node scripts/capture-26939-read-error-band.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'

const OUT = process.argv[2] || '../temp-screenshots/26939'
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = `http://127.0.0.1:${server.httpServer.address().port}`

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['light', 'dark']) {
  const page = await browser.newPage({ viewport: { width: 430, height: 320 }, deviceScaleFactor: 2 })
  page.on('pageerror', (e) => { console.error('pageerror:', e.message); failures++ })
  await page.goto(`${BASE}/capture/crew-dashboard-read-error.html?theme=${theme}`, { waitUntil: 'domcontentloaded' })
  const frame = page.locator('[data-testid="crew-dashboard-iframe"]')
  await frame.waitFor({ state: 'visible', timeout: 15000 })
  const before = await frame.elementHandle()
  // Not awaited: the re-read's own promise settles on the client's schedule, and
  // the band is what the shot waits for.
  await page.evaluate(() => { void window.__rereadDashboard() })
  await page.locator('[data-testid="crew-dashboard-read-error-band"]').waitFor({ state: 'visible', timeout: 15000 })
  const after = await frame.elementHandle()
  check(`${theme}: same iframe element kept`, await page.evaluate(([a, b]) => a === b, [before, after]))
  check(`${theme}: no error card`, (await page.locator('[data-testid="crew-dashboard-error"]').count()) === 0)
  check(`${theme}: retry on the band`, (await page.locator('[data-testid="crew-dashboard-read-error-retry"]').count()) === 1)
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${theme}: palette kiro-${theme}`, painted === `kiro-${theme}`)
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/read-error-band-${theme}.png` })
  await page.close()
}

await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/`)
