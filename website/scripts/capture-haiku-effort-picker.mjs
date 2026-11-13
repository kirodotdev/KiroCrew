/** Real-browser evidence on capture/haiku-effort-picker.html: the model
 * picker's reasoning-effort block on claude-haiku-5.5 and claude-haiku-4.5.
 *
 * The page mounts the REAL ModelEffortDropdown on each Haiku id with
 * `hasEffort` from the REAL modelSupportsEffort(). Each frame asserts which
 * picker carries the effort slider before writing, so a frame cannot document
 * the wrong state.
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light):
 *   haiku-effort-picker-after-<theme>.png    haiku-5.5 has the slider, haiku-4.5 does not
 *   haiku-effort-picker-before-<theme>.png   (--before) neither has it
 *
 * Usage (in website/):
 *   node scripts/capture-haiku-effort-picker.mjs ../temp-screenshots/haiku-effort-picker [--before]
 * `--before` runs the checks the other way round, for a frame taken on a tree
 * whose lib/effort.ts still refuses every Haiku id.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/haiku-effort-picker')
const BEFORE = process.argv.includes('--before')
const MODE = BEFORE ? 'before' : 'after'
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
const LEVELS = ['low', 'medium', 'high', 'xhigh', 'max']
let failures = 0
const check = (label, ok) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 760, height: 640 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  // Gateway-free: answer every API call the mounted picker makes.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/effort-levels') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(LEVELS) })
    }
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/haiku-effort-picker.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root] [data-episode] [role="dialog"]', { timeout: 60000 })
  await page.waitForTimeout(500)

  const hasEffort = async episode => page.locator(`[data-episode="${episode}"]`).getAttribute('data-has-effort')
  // The embedded effort block is the only slider inside a picker.
  const sliders = async episode => page.locator(`[data-episode="${episode}"] [role="dialog"] [role="slider"], [data-episode="${episode}"] [role="dialog"] input[type="range"]`).count()
  const new55 = { flag: await hasEffort('haiku-5.5'), sliders: await sliders('haiku-5.5') }
  const old45 = { flag: await hasEffort('haiku-4.5'), sliders: await sliders('haiku-4.5') }
  console.log(theme, MODE, JSON.stringify({ new55, old45 }))
  if (BEFORE) {
    check(`[${theme}] before: haiku-5.5 picker has no effort block`, new55.flag === 'false' && new55.sliders === 0)
  } else {
    check(`[${theme}] haiku-5.5 picker has the effort block`, new55.flag === 'true' && new55.sliders > 0)
  }
  check(`[${theme}] haiku-4.5 picker has no effort block`, old45.flag === 'false' && old45.sliders === 0)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.locator('[data-capture-root]').screenshot({ path: resolve(OUT, `haiku-effort-picker-${MODE}-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
