/**
 * Captures the markdown-table horizontal-overflow cue (issue #15169) in its
 * four states — table fits (no fade), right edge clipped, both edges clipped
 * mid-scroll, left fade after scrolling to the end — at a 390px phone viewport
 * in light and dark themes.
 *
 * Drives the ISOLATED capture entry (website/capture/table-overflow-cue.html),
 * which mounts the REAL MarkdownRenderer — and so the real MarkdownTable with
 * its shipped `mask-image` fade — over a genuinely overflowing table, so the
 * browser computes real geometry and the screenshot is evidence of the
 * component itself, not of a hand-copied reproduction. The unit suite
 * (src/test/MarkdownRenderer.tableOverflowCue.test.tsx) pins the derivation;
 * happy-dom lays nothing out and drops the inline mask, so the rendered fade is
 * only observable here.
 *
 * Beyond the screenshots it ASSERTS the fade tracks the clip, reading the
 * scroller's `data-overflow` mirror, so a before/after frame cannot silently
 * agree: fits -> '', at rest -> 'right', mid-scroll -> 'both', scrolled to the
 * end -> 'left'.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-table-overflow-cue.mjs http://127.0.0.1:6812 ../temp-screenshots/table-overflow-cue
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import assert from 'node:assert/strict'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/table-overflow-cue'
mkdirSync(OUT, { recursive: true })

// mise's node injects LD_LIBRARY_PATH at its bundled libstdc++, older than the
// system Mesa needs; children inherit it, so scrub it before launching.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const overflowOf = (page) => page.evaluate(() =>
  document.querySelector('[data-testid="table-scroller"]')?.getAttribute('data-overflow'))

try {
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 390, height: 440 }, deviceScaleFactor: 2 })

    // State 1: wide table at rest — right edge clipped, right fade only.
    await page.goto(`${BASE}/capture/table-overflow-cue.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-testid="table-scroller"] table')
    await page.waitForTimeout(80)
    const atRest = await overflowOf(page)
    if (atRest !== 'right') { console.error(`FAIL ${theme}: at rest expected 'right', got '${atRest}'`); failures++ }
    await page.screenshot({ path: `${OUT}/${theme}-390-right-clipped.png` })

    // State 2: scrolled to the middle — both edges clipped, fade on both sides.
    await page.evaluate(() => { const s = window.__scroller(); s.scrollLeft = Math.round((s.scrollWidth - s.clientWidth) / 2); s.dispatchEvent(new Event('scroll')) })
    await page.waitForTimeout(80)
    const mid = await overflowOf(page)
    if (mid !== 'both') { console.error(`FAIL ${theme}: mid-scroll expected 'both', got '${mid}'`); failures++ }
    await page.screenshot({ path: `${OUT}/${theme}-390-both-mid-scroll.png` })

    // State 3: scrolled to the far end — left fade only.
    await page.evaluate(() => { const s = window.__scroller(); s.scrollLeft = s.scrollWidth; s.dispatchEvent(new Event('scroll')) })
    await page.waitForTimeout(80)
    const scrolled = await overflowOf(page)
    if (scrolled !== 'left') { console.error(`FAIL ${theme}: scrolled expected 'left', got '${scrolled}'`); failures++ }
    await page.screenshot({ path: `${OUT}/${theme}-390-left-after-scroll.png` })

    // State 3: a narrow table that fits — no fade on either edge.
    await page.goto(`${BASE}/capture/table-overflow-cue.html?theme=${theme}&fits`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-testid="table-scroller"] table')
    await page.waitForTimeout(80)
    const fits = await overflowOf(page)
    if (fits !== '') { console.error(`FAIL ${theme}: fits expected '', got '${fits}'`); failures++ }
    await page.screenshot({ path: `${OUT}/${theme}-390-fits-no-cue.png` })

    await page.close()
  }
} finally { await browser.close() }

if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN — fade tracked the clip in both themes; screenshots written to', OUT)
