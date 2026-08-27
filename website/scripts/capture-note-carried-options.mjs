/**
 * Real-layout screenshots + assertions for note-carried `[OPTIONS:]` (#5737).
 *
 * Drives website/capture/note-carried-options.html, which mounts the REAL
 * `inject` registry renderer and the REAL FollowUpBar and exposes
 * window.__measure().
 *
 * The unit suites pin both halves (src/test/AppSdkMessageRenderersCov80.test.tsx
 * for the bubble strip, src/test/deriveFollowUpOptions.test.ts for the gate), but
 * neither can show a reviewer that the marker leaves the bubble at the same
 * moment the pills appear. That co-occurrence is the reviewable claim, so it is
 * measured against a real browser here and the frames become the PR evidence.
 *
 * Assertions, one per arm so no arm can pass for another's reason:
 *  - note/off: marker VISIBLE in the bubble AND pills present -> the duplicate
 *    render that was flagged. Asserted so the before frame is proven, not assumed.
 *  - note/on:  marker ABSENT from the bubble AND pills still ['Fix','Skip'] --
 *    both, because a strip that also ate the choices would satisfy only the first.
 *  - cron/off: pills present on a NON-note inject row -> the pre-fix wrong gate.
 *  - cron/on:  pills EMPTY, and the row's own text still intact (the marker is
 *    prose there, so stripping it would be information loss).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-note-carried-options.mjs http://127.0.0.1:6812 ../temp-screenshots/note-carried-options
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/note-carried-options'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1100, height: 420 }
/** The chips' staggered entrance is still translating them for ~750ms after mount. */
const ENTRANCE_SETTLE_MS = 900
const EXPECTED_PILLS = ['Fix', 'Skip']

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

for (const scene of ['note', 'cron']) {
  for (const fix of ['off', 'on']) {
    const page = await browser.newPage({ viewport: VIEWPORT })
    await page.goto(`${BASE}/capture/note-carried-options.html?scene=${scene}&fix=${fix}&theme=dark`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-bubble]')
    await page.waitForTimeout(ENTRANCE_SETTLE_MS)
    const m = await page.evaluate(() => window.__measure())
    console.log(
      `${scene}/${fix}: markerInBubble=${String(m.markerVisibleInBubble).padEnd(5)} ` +
      `pills=[${m.pills.join(', ')}] bubble="${m.bubbleText.slice(0, 72)}"`,
    )

    const pillsMatch = JSON.stringify(m.pills) === JSON.stringify(EXPECTED_PILLS)
    if (scene === 'note' && fix === 'off') {
      if (!m.markerVisibleInBubble) fail('note/off did not reproduce the marker leak — the before frame would be meaningless')
      if (!pillsMatch) fail(`note/off should still show pills (the duplicate render), got [${m.pills.join(', ')}]`)
    }
    if (scene === 'note' && fix === 'on') {
      if (m.markerVisibleInBubble) fail('note/on still prints the marker in the bubble')
      if (!pillsMatch) fail(`note/on lost the choices: expected [${EXPECTED_PILLS.join(', ')}], got [${m.pills.join(', ')}]`)
    }
    if (scene === 'cron' && fix === 'off') {
      if (!pillsMatch) fail('cron/off did not reproduce the wrong-gate pills — the before frame would be meaningless')
    }
    if (scene === 'cron' && fix === 'on') {
      if (m.pills.length) fail(`cron/on gave pills to a non-note inject row: [${m.pills.join(', ')}]`)
      if (!m.markerVisibleInBubble) fail('cron/on stripped a marker that is prose on a non-note row — information loss')
    }

    await page.screenshot({ path: `${OUT}/${scene}-${fix === 'off' ? 'before' : 'after'}.png` })
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
