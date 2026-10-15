/** Real-browser evidence for #16617 on the REAL Members page: the DM header's
 * identity chip names the speaker, the thread's bubbles carry no author line,
 * and the run's last bubble still shows the message time in its hover footer
 * (same affordance as the single-chat page; always visible under hover:none).
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar
 * (deep link into the thread); the API is route-intercepted, gateway-free.
 *
 * Frames per theme (dark, light):
 *   members-<theme>.png        the thread as opened: header chip + bubbles
 *   members-<theme>-hover.png  pointer on the last bubble: footer row with time
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6882 --strictPort            # in website/
 *   node scripts/capture-crewmate-run-members.mjs http://127.0.0.1:6882 ../temp-screenshots/crewmate-run
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6882'
const OUT = resolve(process.argv[3] || '../temp-screenshots/crewmate-run')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page)
  await openRadarDm(page, BASE, theme)
  await page.waitForTimeout(400)

  const rows = page.locator('[data-testid="crewmate-message"]')
  check(`[${theme}] three mate bubbles drawn`, await rows.count() === 3)
  check(`[${theme}] no author line on any bubble`, await page.locator('[data-testid="crewmate-author"]').count() === 0)
  // The header names the speaker: the identity pill (face + name) at the top.
  const chip = page.getByTestId('member-identity-pill').first()
  check(`[${theme}] header identity chip present and names Radar`, await chip.count() === 1 && /Radar/.test(await chip.innerText()))
  await page.screenshot({ path: resolve(OUT, `members-${theme}.png`) })

  // Hover the run's last bubble: its footer reveals the time (tabular-nums span
  // titled with the full date), the same affordance the single chat has.
  const last = rows.nth(2)
  await last.hover({ position: { x: 120, y: 20 } }); await page.waitForTimeout(700)
  const time = last.locator('span.tabular-nums[title]').first()
  const visible = await time.count() === 1 && await time.evaluate(el => {
    const row = el.parentElement
    return !!row && getComputedStyle(row).opacity === '1' && el.textContent.trim().length > 0
  })
  check(`[${theme}] hover on the run's last bubble shows its time`, visible)
  await page.screenshot({ path: resolve(OUT, `members-${theme}-hover.png`) })
  check(`[${theme}] no page errors`, errors.length === 0)
  await ctx.close()
}
await browser.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
