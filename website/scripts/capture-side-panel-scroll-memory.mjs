/**
 * Real-layout proof + screenshots for side-panel scroll memory across
 * chat-slot switches (#5701).
 *
 * Drives the ISOLATED capture entry (website/capture/side-panel-scroll-memory.html),
 * which mounts the REAL `ArtifactPanel` (embedded, as SidePanel's TabBody does)
 * and reproduces a slot switch as a true unmount/remount.
 *
 * This is the only check that exercises the real mount lifecycle with real
 * scroll geometry. The unit suite (src/test/useScrollMemory.test.tsx) pins the
 * hook's record/restore contract, but happy-dom computes no layout, so only a
 * browser can show the document actually coming back mid-scroll.
 *
 * Assertions:
 *  - fix=on:  scroll to a mid-document offset, switch to slot B (unmount),
 *    switch back — the body must sit at the same offset again.
 *  - fix=off: the same round-trip must land at the top. A before arm identical
 *    to the after arm is what a silently inert toggle would produce, so the
 *    reproduction is asserted, not assumed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort    # in another shell (website/)
 *   node scripts/capture-side-panel-scroll-memory.mjs http://127.0.0.1:6813 ../temp-screenshots/side-panel-scroll-memory
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/side-panel-scroll-memory'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1280, height: 720 }
/** Mid-document target offset. Far enough from both ends that a restored
 * frame cannot be confused with either the top or a bottom-clamp. */
const TARGET = 1200
/** Restore tolerance in px: the write is exact, but allow sub-pixel and
 * scrollbar-rounding differences across Chromium builds. */
const EPSILON = 2

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const waitForDoc = async (page) => {
  await page.waitForFunction(() => {
    const m = window.__measure()
    return m.found && m.scrollHeight > 4000
  })
}

for (const fix of ['off', 'on']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  page.on('pageerror', e => { console.error(`[fix=${fix}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/side-panel-scroll-memory.html?theme=dark&fix=${fix}`, { waitUntil: 'networkidle' })
  await waitForDoc(page)

  // 1. Scroll mid-document and photograph the position being left behind.
  await page.evaluate(px => window.__scrollTo(px), TARGET)
  await page.waitForTimeout(120)
  const before = await page.evaluate(() => window.__measure())
  await page.screenshot({ path: `${OUT}/fix-${fix}-1-scrolled.png` })

  // 2. Switch to slot B: slot A's tab body unmounts, exactly as a chat-slot
  //    switch removes the previous slot's bucket from the tree.
  await page.evaluate(() => window.__switch('b'))
  await page.waitForFunction(() => !window.__measure().found)

  // 3. Switch back and photograph where the document lands.
  await page.evaluate(() => window.__switch('a'))
  await waitForDoc(page)
  await page.waitForTimeout(120)
  const after = await page.evaluate(() => window.__measure())
  await page.screenshot({ path: `${OUT}/fix-${fix}-2-returned.png` })

  const expectRestored = fix === 'on'
  const restored = Math.abs(after.scrollTop - TARGET) <= EPSILON
  const atTop = after.scrollTop === 0
  const ok = expectRestored ? restored : atTop
  console.log(`[fix=${fix}] left at ${before.scrollTop}px -> returned at ${after.scrollTop}px `
    + `(scrollHeight ${after.scrollHeight}) => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done — evidence in ${OUT}`)
