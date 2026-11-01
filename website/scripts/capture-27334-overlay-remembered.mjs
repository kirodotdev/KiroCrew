/** Real-browser evidence on the REAL Members page (issue #27334): on a desktop
 * window too narrow to dock the side panel (880px, under the 904px dock
 * threshold with the roster folded), an overlay the user opened is still up
 * after the page is loaded again.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port). One browser context per theme,
 * so localStorage carries from the first load to the second, as it does
 * between two visits.
 *
 * Frames per theme (dark, light), the top 84px of the 880px window on the
 * second load (the header and the overlay's tab strip):
 *   <stage>-second-visit-<theme>.png
 * where <stage> is `after` (the default) or `before`. The before frames are
 * this script run against origin/main's useMembersPageLayout.ts; its checks
 * then fail, since the overlay is closed again.
 *
 * Usage (in website/):
 *   node scripts/capture-27334-overlay-remembered.mjs ../temp-screenshots/27334 [after|before]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/27334')
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
  const ctx = await browser.newContext({ viewport: { width: 880, height: 640 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page)

  // First visit: the panel cannot dock at this width, so the opener raises it
  // as an overlay.
  await openRadarDm(page, BASE, theme)
  const panel = page.getByTestId('member-side-panel')
  check(`[${theme}] first visit opens with no overlay`, await panel.count() === 0)
  await page.getByTestId('member-panel-toggle').click()
  await panel.waitFor()
  check(`[${theme}] the panel is an overlay`, await panel.getAttribute('data-placement') === 'overlay')

  // Second visit: the same page, loaded again.
  await openRadarDm(page, BASE, theme)
  await page.waitForTimeout(500)
  const up = await panel.count() === 1
  check(`[${theme}] second visit: the overlay is still up`, up)
  if (up) check(`[${theme}] second visit: still an overlay`, await panel.getAttribute('data-placement') === 'overlay')
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  // The header band and the overlay's tab strip: enough to show whether the
  // panel came back. The panel body below is left out, as this gateway-free
  // fixture serves no dashboard page for it to draw.
  await page.screenshot({ path: resolve(OUT, `${STAGE}-second-visit-${theme}.png`), clip: { x: 0, y: 0, width: 880, height: 84 } })
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
