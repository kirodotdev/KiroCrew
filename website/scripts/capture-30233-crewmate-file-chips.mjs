/** Real-browser evidence on the REAL Members page: a crewmate's chat draws no
 * "N files changed" chips under a reply whose turn wrote files (#30233).
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), full viewport:
 *   crewmate-file-chips-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-30233-crewmate-file-chips.mjs <out-dir> [--expect-chips]
 * `--expect-chips` runs the checks the other way round, for a "before" frame
 * taken on a tree that still draws the chips.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crewmate-file-chips')
const EXPECT_CHIPS = process.argv.includes('--expect-chips')
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

/** The last reply's turn wrote the mate's notes file. */
const last = THREAD[THREAD.length - 1]
const WROTE_THREAD = [
  ...THREAD.slice(0, -1),
  {
    ...last,
    meta: {
      ...last.meta,
      file_changes: [{ path: '/home/me/.kiro/crew/members/radar/notes.md', before: '# Notes\n', after: '# Notes\n\n- Dispatcher back up 08:22Z\n' }],
    },
  },
]

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: WROTE_THREAD })
  await openRadarDm(page, BASE, theme)
  await page.getByText('it no longer mints an owner token').waitFor()

  const chips = await page.locator('[data-testid^="fcc-row-"], [data-testid^="fcc-header-"]').count()
  check(`[${theme}] file-change chips drawn: ${chips}`, EXPECT_CHIPS ? chips > 0 : chips === 0)
  check(`[${theme}] the crewmate's reply still draws`, (await page.getByText('it no longer mints an owner token').count()) === 1)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.screenshot({ path: resolve(OUT, `crewmate-file-chips-${theme}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
