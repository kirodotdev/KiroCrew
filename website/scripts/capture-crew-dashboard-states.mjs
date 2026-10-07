/** Real-browser evidence for the Dashboard tab's own states.
 *
 * Drives website/capture/crew-dashboard-states.html, which mounts the REAL
 * `CrewDynamicDashboard` over a gateway stubbed only at its one read. Each shot
 * asserts the branch it claims to photograph BEFORE the screenshot, so an image
 * can never show a state the component is not actually in.
 *
 * Boots its own dev server in-process on an ephemeral port, so it is ONE command
 * with no second shell to keep alive and no fixed port to collide with:
 *   node scripts/capture-crew-dashboard-states.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'

const OUT = process.argv[2] || '../.github/screenshots/dyndash'
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  // Port 0 lets the OS pick, so two runs can overlap and neither owns a number.
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const addr = server.httpServer.address()
const BASE = `http://127.0.0.1:${addr.port}`
console.log(`serving ${BASE}`)

// The side panel's own width, which is where a reader meets this tab.
const VIEWPORT = { width: 430, height: 320 }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

/** One shot: open a state in a theme, assert its branch, write the png. */
async function shoot({ state, theme, name, present, absent }) {
  const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  page.on('pageerror', e => {
    console.error('pageerror:', e.message)
    failures++
  })
  await page.goto(`${BASE}/capture/crew-dashboard-states.html?state=${state}&theme=${theme}`, {
    waitUntil: 'domcontentloaded',
  })
  const want = page.locator(`[data-testid="${present}"]`)
  await want.waitFor({ state: 'visible', timeout: 15000 })
  check(`${name}: ${present} is on screen`, (await want.count()) === 1)
  // The THEME is asserted, not assumed. `ThemeProvider` seeds its own mode on
  // mount, so a shot can silently come out in the other palette and the only
  // evidence of it is two files with identical bytes.
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${name}: palette is kiro-${theme}, got ${painted}`, painted === `kiro-${theme}`)
  // The branch is pinned by what is NOT there as well: the empty state exists to
  // stop a retry being offered for an answer a retry cannot change.
  for (const id of absent) {
    check(`${name}: ${id} is absent`, (await page.locator(`[data-testid="${id}"]`).count()) === 0)
  }
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.close()
}

// Nothing adopted: a state, not a failure. No error notice and no retry.
await shoot({
  state: 'none',
  theme: 'dark',
  name: 'dyndash-state-none-dark',
  present: 'crew-dashboard-none',
  absent: ['crew-dashboard-empty', 'crew-dashboard-empty-retry', 'crew-dashboard-error'],
})
await shoot({
  state: 'none',
  theme: 'light',
  name: 'dyndash-state-none-light',
  present: 'crew-dashboard-none',
  absent: ['crew-dashboard-empty', 'crew-dashboard-empty-retry', 'crew-dashboard-error'],
})

// NOT captured here: the kept-page and mint-error bands. Both are reached through
// the frame's probation machinery -- a newer document minted, probed and refused --
// not through a second read, so posing them from a stubbed read would photograph a
// band this component never put there. Their copy is pinned by the unit tests.

await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/`)
