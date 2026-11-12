/** Real-browser evidence on capture/kas-signed-out-card.html: the KAS
 * signed-out row reads in the chosen language, points at its sign-in button
 * without naming the engine's route, and a row written before the token existed
 * reads the same.
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Frames, element-scoped: kas-signed-out-<lang>-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-kas-signed-out-card.mjs ../temp-screenshots/kas-signed-out
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/kas-signed-out')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
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

for (const [lang, theme] of [['ja', 'dark'], ['ja', 'light'], ['en', 'dark']]) {
  const ctx = await browser.newContext({ viewport: { width: 800, height: 600 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/kas-signed-out-card.html?theme=${theme}&lang=${lang}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root] [data-testid="error-card"]', { timeout: 60000 })
  const card = id => page.locator(`[data-scene="${id}"] [data-testid="error-card"]`).first()
  for (const id of ['token', 'history']) {
    const text = (await card(id).innerText()).trim()
    console.log(lang, theme, id, JSON.stringify(text))
    if (lang === 'ja') check(`[${lang}/${theme}/${id}] reads Japanese`, /サインインしていません/.test(text) && !/Not signed in/.test(text))
    else check(`[${lang}/${theme}/${id}] reads English`, /Not signed in to Kiro/.test(text))
    check(`[${lang}/${theme}/${id}] points at the button, names no route`, !/relay|リレー|KAS|→/.test(text))
    check(`[${lang}/${theme}/${id}] keeps the sign-in button`, (await card(id).locator('[data-testid="error-card-sign-in"]').count()) === 1)
  }
  check(`[${lang}/${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))
  await page.locator('[data-capture-root]').screenshot({ path: resolve(OUT, `kas-signed-out-${lang}-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
