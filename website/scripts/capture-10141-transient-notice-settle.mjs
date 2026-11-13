/** Real-browser evidence on capture/transient-notice-settle.html:
 * a pending transient-retry notice settles to the past-tense "Connection
 * interrupted" when the ladder gives up (or another error ends it), and the
 * resuming notice names the answer it is finishing.
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), element-scoped:
 *   transient-notice-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-10141-transient-notice-settle.mjs ../temp-screenshots/10141 [--before]
 * `--before` runs the checks the OTHER way round, for a frame taken on a tree
 * that still leaves the notice reading "retrying…" above the give-up.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/10141')
const BEFORE = process.argv.includes('--before')
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
  await page.goto(`${BASE}/capture/transient-notice-settle.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root] [data-testid="notice-card"]', { timeout: 60000 })
  await page.waitForTimeout(500)

  const scene = id => page.locator(`[data-scene="${id}"]`)
  const notice = id => scene(id).locator('[data-testid="notice-card"]').first()
  const read = async id => ({
    text: (await notice(id).innerText()).trim(),
    tone: await notice(id).getAttribute('data-tone'),
  })
  const giveUp = await read('give-up')
  const other = await read('other')
  const resuming = await read('resuming')
  const restored = await read('restored')
  console.log(theme, JSON.stringify({ giveUp, other, resuming, restored }))
  if (BEFORE) {
    check(`[${theme}] before: give-up scene still reads retrying`, /retrying…/.test(giveUp.text) && giveUp.tone === 'warn')
    check(`[${theme}] before: other-error scene still reads retrying`, /retrying…/.test(other.text))
    check(`[${theme}] before: resuming copy is the old one`, /resuming…/.test(resuming.text))
  } else {
    check(`[${theme}] give-up scene reads interrupted (info)`, /Connection interrupted/.test(giveUp.text) && giveUp.tone === 'info')
    check(`[${theme}] other-error scene reads interrupted (info)`, /Connection interrupted/.test(other.text) && other.tone === 'info')
    check(`[${theme}] resuming names its answer (warn)`, /finishing this answer…/.test(resuming.text) && resuming.tone === 'warn')
  }
  check(`[${theme}] restored control unchanged`, /Connection restored/.test(restored.text) && restored.tone === 'info')
  check(`[${theme}] give-up row still a red ErrorCard`, (await scene('give-up').locator('[data-testid="error-card"]').count()) === 1)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.locator('[data-capture-root]').screenshot({ path: resolve(OUT, `transient-notice-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
console.log(`wrote ${OUT}`)
