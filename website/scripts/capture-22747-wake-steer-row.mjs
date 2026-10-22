/** Real-browser evidence on the REAL Members page (#22747): a work-ledger wake
 * steered into the crewmate's running turn no longer draws as a bubble the
 * person typed.
 *
 * The same Radar DM thread, with the wake row the gateway persists appended:
 *   before-<theme>.png  the row main wrote: role `user` + `meta.steer` (a bubble)
 *   after-<theme>.png   the row this PR writes: role `nudge` + `meta.steer`
 *
 * Asserted before each PNG: the wake text is on screen in `before` and absent in
 * `after` (the crewmate DM draws messages only, so a nudge row draws nothing).
 *
 * Usage (in website/):
 *   node scripts/capture-22747-wake-steer-row.mjs ../temp-screenshots/22747-wake-steer-row
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/22747-wake-steer-row')
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

const WAKE = '[work-ledger wake] item=it_89256839 status=question'
const wakeRow = role => ({
  role, content: `${WAKE}\n[work-ledger] read the ledger with work_ledger_read`, ts: '2026-10-05T07:52:30Z',
  meta: { mid: 'm-wake', steer: true, steerState: 'consumed', ...(role === 'nudge' ? { nudge: { cycle: 4, loop_id: 'loop-1' } } : {}) },
})
const SCENES = [['before', wakeRow('user')], ['after', wakeRow('nudge')]]

for (const theme of ['dark', 'light']) {
  for (const [name, row] of SCENES) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await routeRadarDm(page, { thread: [THREAD[0], THREAD[1], THREAD[3], row, THREAD[4]] })
    await openRadarDm(page, BASE, theme)
    await page.getByText('Back up since 08:22Z', { exact: false }).last().waitFor()
    const shown = await page.getByText(WAKE, { exact: false }).count()
    check(`[${theme}/${name}] wake drawn as a bubble: ${shown > 0}`, name === 'before' ? shown > 0 : shown === 0)
    check(`[${theme}/${name}] no page errors`, errors.length === 0)
    if (errors.length) console.log(errors.join('\n'))
    // The chat column only: the side panel has no fixture and is not the subject.
    await page.screenshot({ path: resolve(OUT, `${name}-${theme}.png`), clip: { x: 0, y: 0, width: 640, height: 720 } })
    await ctx.close()
  }
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
