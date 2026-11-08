/**
 * Screenshots + assertions for #23572: "Optimize prompt" stays reachable while
 * a turn runs.
 *
 * Drives the isolated capture entry (website/capture/optimize-while-busy.html),
 * which mounts the REAL ChatInput in four states: idle with a draft, a running
 * turn with a draft (queue), the same with steer available (split button), and
 * a running turn with an empty composer (Stop).
 *
 * Mid-turn the send cluster is at its button cap (max-two-buttons-per-row), so
 * Optimize is a row in the "+" menu there, and the cluster's own button only
 * when idle. Asserted before each PNG:
 *   rows.png         per row: the send-slot control is present; the cluster
 *                    has an Optimize button only on the idle row.
 *   menu-<row>.png   the "+" menu opened on a busy row holds an "Optimize
 *                    prompt" row, enabled with a draft and disabled without.
 * Pass --record-only to photograph a tree without asserting (main, where no
 * busy row offers Optimize anywhere).
 *
 * Starts its own loopback vite dev server.
 *
 * Usage (in website/):
 *   node scripts/capture-23572-optimize-while-busy.mjs ../temp-screenshots/23572 after
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

const args = process.argv.slice(2).filter(a => !a.startsWith('--'))
const OUT = args[0] || '../temp-screenshots/23572-optimize-while-busy'
const NAME = args[1] || 'after'
const RECORD_ONLY = process.argv.includes('--record-only')
mkdirSync(OUT, { recursive: true })

// Its own loopback dev server on a free port, closed at the end.
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

/** episode id -> [send-slot locator, cluster Optimize button?, menu row enabled? (null: no row)] */
const EXPECTED = {
  idle: ['button[aria-label="Send"]', true, null],
  queue: ['button[aria-label="Queue message"]', false, true],
  steer: ['[data-testid="busy-send-button"]', false, true],
  stop: ['[data-testid="stop-button-armed"]', false, false],
}

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} -> ${ok ? 'ok' : 'FAIL'}`)
  if (!ok && !RECORD_ONLY) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 900, height: 620 }, deviceScaleFactor: 2 })
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/optimize-while-busy.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-episode="stop"] [aria-label="Message input"]')

  for (const [episode, [slot, clusterButton]] of Object.entries(EXPECTED)) {
    const row = page.locator(`[data-episode="${episode}"]`)
    const slotCount = await row.locator(slot).count()
    const buttons = await row.getByRole('button', { name: 'Optimize prompt' }).count()
    check(`${theme} ${episode.padEnd(6)} slot=${slotCount} clusterOptimize=${buttons}`, slotCount >= 1 && buttons === (clusterButton ? 1 : 0))
  }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${NAME}-rows-${theme}.png` })

  for (const [episode, [, , menuEnabled]] of Object.entries(EXPECTED)) {
    if (menuEnabled === null) continue
    const row = page.locator(`[data-episode="${episode}"]`)
    await row.getByTitle('Add files & options').click()
    const item = page.getByTestId('composer-menu-optimize')
    const present = (await item.count()) === 1
    const enabled = present && (await item.isEnabled())
    check(`${theme} ${episode.padEnd(6)} menuOptimize=${present ? (enabled ? 'enabled' : 'disabled') : 'absent'}`, present && enabled === menuEnabled)
    // The row and the menu above it, which portals out of the row.
    const box = await row.boundingBox()
    const top = Math.max(0, box.y - 470)
    await page.screenshot({ path: `${OUT}/${NAME}-menu-${episode}-${theme}.png`, clip: { x: 0, y: top, width: 900, height: box.y + box.height - top + 8 } })
    await page.keyboard.press('Escape')
    await page.mouse.click(5, 5)
  }
  if (errors.length) { console.error(errors.join('\n')); failures++ }
  await page.close()
}

await browser.close()
await server.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed.`)
  process.exit(1)
}
console.log('\nall rows render as specified.')
