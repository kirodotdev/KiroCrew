/** Real-browser evidence on the REAL Members page (issue #27335): hovering the
 * crewmate header's side-panel opener shows its name at once, in the same
 * InstantTip the chat page's side-panel toggle uses, instead of waiting out
 * the browser's native title delay.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port). The docked panel starts hidden
 * (`mc-members-panel-open` = 0) so the header shows its opener.
 *
 * Frames per theme (dark, light), clipped to the header's right end, 300ms
 * after the pointer lands (past the tip's 100ms intent delay, well before a
 * native title would show):
 *   <stage>-hover-<theme>.png
 * where <stage> is `after` (the default) or `before`. The before frames are
 * this script run against origin/main's MembersPageThreadHeader.tsx; its
 * checks then fail, since no tip shows.
 *
 * Usage (in website/):
 *   node scripts/capture-27335-panel-toggle-tip.mjs ../temp-screenshots/27335 [after|before]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/27335')
const STAGE = process.argv[3] === 'before' ? 'before' : 'after'
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

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  await ctx.addInitScript(() => window.localStorage.setItem('mc-members-panel-open', '0'))
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page)
  await openRadarDm(page, BASE, theme)

  const toggle = page.getByTestId('member-panel-toggle')
  await toggle.waitFor()
  await toggle.hover()
  await page.waitForTimeout(300)
  const tip = page.getByRole('tooltip')
  const shown = await tip.count() === 1 && await tip.isVisible()
  check(`[${theme}] tip shown 300ms after hover`, shown)
  if (shown) check(`[${theme}] tip text is the control's name`, (await tip.textContent()) === await toggle.getAttribute('aria-label'))
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  // The header's right end: the opener sits at the top-right corner, so the
  // tip opens below it, inside this band.
  const clip = { x: 1280 - 420, y: 0, width: 420, height: 110 }
  await page.screenshot({ path: resolve(OUT, `${STAGE}-hover-${theme}.png`), clip })
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
