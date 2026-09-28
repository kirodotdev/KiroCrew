/**
 * Real-behavior capture + assertion for PR #7616 — un-toggling a follow-up chip
 * removes ONLY the chip-appended suffix, never user-typed text that equals it.
 *
 * Drives the isolated capture entry (website/capture/chip-suffix-untoggle.html),
 * which mounts the REAL FollowUpBar wired to the SAME toggle algorithm the
 * product ships (fix=on) and to the verbatim pre-fix algorithm (fix=off).
 *
 * Scenario (both arms):
 *   1. click the "Alpha" chip                 → composer "Alpha"
 *   2. type over the draft: "other, Alpha"    → the user's own text, same tail
 *   3. click the lit "Alpha" chip to un-toggle
 * Expected: fix=on keeps "other, Alpha"; fix=off deletes it to "other".
 *
 * The before arm is ASSERTED to reproduce the deletion, so the before/after
 * evidence is meaningful rather than two identical frames.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-chip-suffix-untoggle.mjs http://127.0.0.1:6813 ../temp-screenshots/chip-suffix-untoggle
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/chip-suffix-untoggle'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 900, height: 360 }

// mise's node injects an older LD_LIBRARY_PATH; children inherit it, so scrub
// it here (mirrors capture/followup-chip-columns). A PW_BROWSER_LDPATH override
// (used on hosts where Chromium's system libs were extracted to a local prefix)
// is honoured when set.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
if (process.env.PW_BROWSER_LDPATH) browserEnv.LD_LIBRARY_PATH = process.env.PW_BROWSER_LDPATH
const browser = await chromium.launch({ env: browserEnv, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] })
let failures = 0

for (const fix of ['off', 'on']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/chip-suffix-untoggle.html?theme=dark&fix=${fix}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-composer]')

  const chip = page.getByRole('button', { name: 'Alpha', exact: true })
  const composer = page.locator('[data-composer]')

  // 1. Pick Alpha → the chip appends its own suffix. The chip carries a 220ms
  // single/double-click debounce, so let it elapse before reading.
  await chip.click()
  await page.waitForFunction(() => window.__state().value === 'Alpha', null, { timeout: 5000 })

  // 2. The user rewrites the whole draft to their OWN text ending ", Alpha".
  await composer.fill('other, Alpha')
  await page.waitForFunction(() => window.__state().value === 'other, Alpha', null, { timeout: 5000 })

  // Snapshot the pre-un-toggle state (both arms are identical here).
  await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-1-typed.png` })

  // 3. Un-toggle the still-lit Alpha chip. Wait past the debounce for onSelect.
  await chip.click()
  await page.waitForTimeout(400)
  const value = await page.evaluate(() => window.__state().value)
  await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-2-untoggled.png` })

  console.log(`fix=${fix.padEnd(3)}: after un-toggle composer = ${JSON.stringify(value)}`)

  if (fix === 'on' && value !== 'other, Alpha') {
    console.error(`FAIL(fix=on): expected "other, Alpha" (user text preserved), got ${JSON.stringify(value)}`)
    failures++
  }
  if (fix === 'off' && value !== 'other') {
    console.error(`FAIL(fix=off): the before arm must reproduce the deletion (expected "other"), got ${JSON.stringify(value)} — before/after evidence would be meaningless`)
    failures++
  }
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
