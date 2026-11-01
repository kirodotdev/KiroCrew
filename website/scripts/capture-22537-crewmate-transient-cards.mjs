/** Real-browser evidence on the REAL Members page: a crewmate's chat draws no
 * card for the transient-5xx ladder's pending links (#22537). A patroller whose
 * wakes each hit a retry used to stack one "Connection restored" card per wake
 * with nothing of its own between them; now only its speech and the user's
 * rows draw. The terminal give-up (red ErrorCard) still draws.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), full viewport:
 *   crewmate-transient-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-22537-crewmate-transient-cards.mjs ../temp-screenshots/crewmate-transient [--expect-cards]
 * `--expect-cards` runs the checks the OTHER way round, for a "before" frame
 * taken on a tree that still draws the cards.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crewmate-transient')
const EXPECT_CARDS = process.argv.includes('--expect-cards')
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

/** Four patrol wakes, each re-queued once by the ladder and answered with the
 *  say-nothing row; then the user asks and the crewmate answers. */
const wake = (n) => {
  const t = (s) => `2026-10-20T1${n}:00:${String(s).padStart(2, '0')}Z`
  return [
    { role: 'nudge', content: `[auto-nudge cycle ${n}]\nPatrol.`, ts: t(0), meta: { nudge: { cycle: n } } },
    { role: 'error', content: '⟳ Backend hiccup — retrying…', ts: t(2), meta: { notice: 'transient_retrying', kind: 'transient_retry' } },
    { role: 'assistant', content: '\u200B', ts: t(9) },
  ]
}
const TRANSIENT_THREAD = [THREAD[0], THREAD[1], ...wake(1), ...wake(2), ...wake(3), ...wake(4), THREAD[3], THREAD[4]]

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: TRANSIENT_THREAD })
  await openRadarDm(page, BASE, theme)
  await page.getByText('it no longer mints an owner token').waitFor()

  // Either tense of the pending card: "retrying…" while the filtered
  // transcript hides the say-nothing row that would settle it, "restored"
  // once a drawn reply follows. Neither belongs in this chat.
  const cards = await page.getByText(/Connection (unstable|restored)/).count()
  check(`[${theme}] transient-ladder cards drawn: ${cards}`, EXPECT_CARDS ? cards === 4 : cards === 0)
  check(`[${theme}] the crewmate's speech still draws`, (await page.getByText('Shard 1 went red twice').count()) === 1)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.screenshot({ path: resolve(OUT, `crewmate-transient-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
