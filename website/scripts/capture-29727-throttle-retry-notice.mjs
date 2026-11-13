/** Real-browser evidence on capture/throttle-retry-notice.html:
 * a retry caused by a provider throttle reads as throttling while pending and
 * once settled (answered or gave up), including the give-up error card, never
 * as "Connection ...", and a plain
 * upstream 5xx retry still reads "Connection restored".
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), element-scoped:
 *   throttle-notice-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-29727-throttle-retry-notice.mjs ../temp-screenshots/29727
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/29727')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 800, height: 1000 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/throttle-retry-notice.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root] [data-testid="notice-card"]', { timeout: 60000 })
  await page.waitForTimeout(500)

  const scene = id => page.locator(`[data-scene="${id}"]`)
  const notices = async id => {
    const cards = scene(id).locator('[data-testid="notice-card"]')
    const out = []
    for (let i = 0; i < (await cards.count()); i++) {
      out.push({ text: (await cards.nth(i).innerText()).trim(), tone: await cards.nth(i).getAttribute('data-tone') })
    }
    return out
  }
  const pending = await notices('pending')
  const answered = await notices('answered')
  const gaveUp = await notices('gave-up')
  const resuming = await notices('resuming')
  const plain = await notices('plain-5xx')
  console.log(theme, JSON.stringify({ pending, answered, resuming, gaveUp, plain }))
  check(`[${theme}] pending reads throttling (warn)`, pending.length === 1 && /throttling requests — retrying…/.test(pending[0].text) && pending[0].tone === 'warn')
  check(`[${theme}] answered rows read "Throttling cleared" (info)`, answered.length === 3 && answered.every(n => /Throttling cleared/.test(n.text) && n.tone === 'info'))
  check(`[${theme}] resuming reads throttling (warn)`, resuming.length === 1 && /throttling requests — finishing this answer…/.test(resuming[0].text) && resuming[0].tone === 'warn')
  check(`[${theme}] gave-up row reads kept throttling (info)`, gaveUp.length === 1 && /kept throttling/.test(gaveUp[0].text) && gaveUp[0].tone === 'info')
  const gaveUpCard = (await scene('gave-up').locator('[data-testid="error-card"]').innerText()).trim()
  check(`[${theme}] throttle give-up card names the throttle`, /throttling requests — please try again\./.test(gaveUpCard))
  check(`[${theme}] no throttle scene mentions a connection`, [...pending, ...answered, ...resuming, ...gaveUp, { text: gaveUpCard }].every(n => !/connection/i.test(n.text)))
  check(`[${theme}] plain 5xx control unchanged`, plain.length === 1 && /Connection restored/.test(plain[0].text))
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.locator('[data-capture-root]').screenshot({ path: resolve(OUT, `throttle-notice-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
console.log(`wrote ${OUT}`)
