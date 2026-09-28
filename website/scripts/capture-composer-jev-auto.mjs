/**
 * Screenshots + assertions for the composer model chip's three states.
 *
 * Drives the isolated capture entry (website/capture/composer-jev-auto.html),
 * which mounts the REAL ChatInput and derives both chip markers from the REAL
 * `jevRouteOffered()` / `isUnpinnedModel()` predicates the two hosts use.
 *
 * The assertions are what make the frame more than decoration: a chip that
 * carried the Jev marker on a PINNED slot, or that showed `· default` where the
 * preview is on, would photograph plausibly and fail here.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6844 --strictPort   # in another shell
 *   node scripts/capture-composer-jev-auto.mjs http://127.0.0.1:6844 ../temp-screenshots/composer-jev-auto
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6844'
const OUT = process.argv[3] || '../temp-screenshots/composer-jev-auto'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 900, height: 520 }

/**
 * episode id -> [must appear in the chip, must NOT appear in the chip].
 *
 * The routed chip must NOT carry a model id: its model changes every turn, so an
 * id there is stale by the next reply. That is the assertion that makes the three
 * states tellable apart -- a policy name, or an id with a marker, never both.
 */
const EXPECTED = {
  pinned: ['gpt-5.6-terra', 'Auto (Jev)'],
  'jev-auto': ['Auto (Jev)', 'gpt-5.6-sol'],
  'plain-auto': ['gpt-5.6-sol \u00B7 default', 'Auto (Jev)'],
}

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/composer-jev-auto.html?theme=${theme}`, {
    waitUntil: 'networkidle',
  })
  await page.waitForSelector('[data-episode="plain-auto"] [data-testid="composer-model-chip"]')

  for (const [episode, [wanted, unwanted]] of Object.entries(EXPECTED)) {
    const chip = page.locator(`[data-episode="${episode}"] [data-testid="composer-model-chip"]`)
    const text = (await chip.innerText()).replace(/\s+/g, ' ').trim()
    const ok = text.includes(wanted) && !text.includes(unwanted)
    console.log(`${theme} ${episode.padEnd(10)}: "${text}" -> ${ok ? 'ok' : 'FAIL'}`)
    if (!ok) {
      console.error(`FAIL: ${theme}/${episode} chip reads "${text}"; want "${wanted}", not "${unwanted}"`)
      failures++
    }
  }

  const shot = `${OUT}/composer-jev-auto-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path: shot })
  console.log(`  -> ${shot}`)
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed.`)
  process.exit(1)
}
console.log('\nall chip states render as specified.')
