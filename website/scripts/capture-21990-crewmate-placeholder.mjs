/** Real-browser evidence on the REAL Members page: the crewmate DM composer's
 * empty-state hint names the crewmate and nothing else ("Message Radar…"),
 * with no parenthesized sigil list after the name.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), clipped to the composer dock and the last
 * bubble above it:
 *   placeholder-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-21990-crewmate-placeholder.mjs ../temp-screenshots/crewmate-placeholder
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crewmate-placeholder')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5197, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

// One bubble per run so the composer has room above it in a 720px frame.
const SHORT_THREAD = [THREAD[0], THREAD[1], THREAD[3], THREAD[4]]

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: SHORT_THREAD })
  await openRadarDm(page, BASE, theme)

  const dock = page.getByTestId('composer-dock')
  const textarea = page.getByTestId('input-wrapper').locator('textarea').first()
  await textarea.waitFor()
  const hint = await textarea.getAttribute('placeholder')
  check(`[${theme}] hint names the crewmate: ${JSON.stringify(hint)}`, hint === 'Message Radar…')
  check(`[${theme}] hint carries no sigil list`, !/\/command|@file|\$skill/.test(hint || ''))
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  const d = await dock.boundingBox()
  const clip = { x: Math.max(0, d.x - 24), y: Math.max(0, d.y - 120), width: Math.min(1280, d.width + 48), height: Math.min(720 - Math.max(0, d.y - 120), d.height + 150) }
  await page.screenshot({ path: resolve(OUT, `placeholder-${theme}.png`), clip })
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
