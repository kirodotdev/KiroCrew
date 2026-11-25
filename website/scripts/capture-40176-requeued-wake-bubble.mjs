/** Real-browser evidence on the REAL Members page (#40176): a work-ledger wake
 * the crewmate's turn did not take, requeued for the next turn, no longer draws
 * as a bubble the person typed while it waits.
 *
 * The Radar DM thread with the steered wake's `nudge` row and its requeued
 * queue entry listed by the slot detail:
 *   before-<theme>.png  the entry as main lists it: no automation mark (a bubble)
 *   after-<theme>.png   the entry as this PR lists it: `meta.automationSteer`
 *
 * Asserted before each PNG: the wake text is on screen in `before` and absent in
 * `after`, and the person's own message still draws in both.
 *
 * Usage (in website/):
 *   node scripts/capture-40176-requeued-wake-bubble.mjs ../temp-screenshots/40176-requeued-wake-bubble
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/40176-requeued-wake-bubble')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

const WAKE = '[work-ledger wake] item=it_9388b67f status=question'
const WAKE_TEXT = `${WAKE}\n\n[work-ledger] read the ledger with work_ledger_read`
const steeredRow = {
  role: 'nudge', content: WAKE_TEXT, ts: '2026-10-05T07:52:30Z',
  meta: { mid: 'm-wake', steer: true, steerState: 'requeued', nudge: { cycle: 70, loop_id: 'loop-1' } },
}
const thread = [THREAD[0], THREAD[1], THREAD[3], THREAD[4], steeredRow]
const entry = meta => ({ id: 'q-wake', content: WAKE_TEXT, ...(meta ? { meta } : {}) })
const SCENES = [['before', entry()], ['after', entry({ automationSteer: true })]]

for (const theme of ['dark', 'light']) {
  for (const [name, queued] of SCENES) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await routeRadarDm(page, { thread })
    // Registered after the fixture's table, so it answers the slot detail first.
    await page.route(u => /^\/api\/chat\/slots\/[^/]+$/.test(new URL(u).pathname), route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ key: 'member-radar', title: 'Radar', running: false, messages: thread, queue: [queued] }),
    }))
    await openRadarDm(page, BASE, theme)
    await page.getByText('Back up since 08:22Z', { exact: false }).last().waitFor()
    const shown = await page.getByText(WAKE, { exact: false }).count()
    check(`[${theme}/${name}] wake drawn as a bubble: ${shown > 0}`, name === 'before' ? shown > 0 : shown === 0)
    check(`[${theme}/${name}] the person's message still draws`, (await page.getByText('Good. And the dispatcher?').count()) > 0)
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
