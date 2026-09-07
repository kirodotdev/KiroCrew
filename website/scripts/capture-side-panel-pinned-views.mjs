/**
 * Real-browser evidence that the side panel's pinned block is PERMANENT, not
 * content-gated (#5377).
 *
 * Drives the ISOLATED capture entry (website/capture/side-panel-pinned-views.html),
 * which mounts the REAL `SidePanel` over the real `usePanelTabs` store with
 * nothing seeded: no pins, no sources, no issues, no artifacts. The strip in the
 * screenshot is therefore produced by SidePanel's own
 * `syncPinned(PINNED_VIEWS)` effect, not by a fixture.
 *
 * The unit suite (src/test/sidePanelPinnedAlwaysPresent.test.tsx) pins the same
 * four properties in happy-dom. This exists for what a DOM assertion cannot
 * carry: a picture of the surface the corrected contract describes, which is the
 * evidence a reader of that contract actually wants.
 *
 * Assertions:
 *  - claimed=off: exactly three chips - Changes, Artifacts, Files, in
 *    PINNED_VIEWS order - and none of them carries a close control.
 *  - claimed=on:  the same harness with `syncPinned([])` applied after mount,
 *    i.e. the content-gated reading of the contract. The strip must be EMPTY.
 *    An arm identical to the other is what an inert toggle would produce, so
 *    the contrast is asserted rather than assumed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort    # in another shell (website/)
 *   node scripts/capture-side-panel-pinned-views.mjs http://127.0.0.1:6821 ../temp-screenshots/side-panel-pinned-views
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/side-panel-pinned-views'
mkdirSync(OUT, { recursive: true })

/** The panel's own dock width, so the capture IS the panel rather than a
 *  sliver of a mostly-empty page. Encodes no selector. */
const VIEWPORT = { width: 460, height: 720 }
/** PINNED_VIEWS order, which is also strip order. */
const EXPECTED = ['Changes', 'Artifacts', 'Files']

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const theme of ['dark', 'light']) {
  for (const claimed of ['off', 'on']) {
    const page = await browser.newPage({ viewport: VIEWPORT })
    page.on('pageerror', e => { console.error(`[${theme}/claimed=${claimed}] pageerror:`, e.message); failures++ })
    await page.goto(`${BASE}/capture/side-panel-pinned-views.html?theme=${theme}&claimed=${claimed}`, { waitUntil: 'networkidle' })

    // Wait for the arm's own settled shape rather than a fixed sleep: the
    // permanent arm settles at three chips, the content-gated arm at zero.
    const want = claimed === 'on' ? 0 : EXPECTED.length
    await page.waitForFunction(n => window.__strip().count === n, want, { timeout: 15000 })
    await page.waitForTimeout(150)

    const strip = await page.evaluate(() => window.__strip())
    // The palette is asserted, not assumed: ThemeProvider owns `data-theme` and
    // an unset preference resolves to the HOST's mode, so a filename claiming a
    // theme the frame does not actually carry is the failure this catches.
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const themeOk = applied === (theme === 'light' ? 'kiro-light' : 'kiro-dark')
    await page.screenshot({ path: `${OUT}/${theme}-${claimed === 'on' ? 'as-documented-empty' : 'actual-always-present'}.png` })

    let ok
    if (claimed === 'on') {
      ok = strip.count === 0
    } else {
      ok = strip.count === EXPECTED.length
        && EXPECTED.every((n, i) => strip.names[i] === n)
        && strip.closable.every(c => c === false)
    }
    ok = ok && themeOk
    console.log(`[${theme}/claimed=${claimed}] ${strip.count} chip(s) ${JSON.stringify(strip.names)} `
      + `closable=${JSON.stringify(strip.closable)} data-theme=${applied} => ${ok ? 'OK' : 'FAIL'}`)
    if (!ok) failures++
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
