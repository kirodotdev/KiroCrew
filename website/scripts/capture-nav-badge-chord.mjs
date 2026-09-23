/**
 * Real-layout overlap probe for the nav rail's right edge (unread badge vs the
 * hover/focus shortcut hint).
 *
 * Drives the ISOLATED capture entry (website/capture/nav-badge-chord.html),
 * which mounts the REAL NavItem/NavBadge. This is the only check that exercises
 * LAYOUT: src/test/App.test.tsx pins the structure (badge in flow, after the
 * chord), but happy-dom computes no geometry, so an overlap returning through a
 * path that leaves the structure intact — a later `absolute` on either box, a
 * wrapper that stops shrinking — is only caught here.
 *
 * Assertions:
 *  - fix=on:  no right-edge element intersects the chord, at any theme, and no
 *             row pushes its chord outside the row box (zero overlap earned by
 *             shoving the hint off the row is not a fix).
 *  - fix=off: the unread badge MUST intersect the chord. A before frame that
 *             matches the after frame is exactly what an override that silently
 *             failed to apply would produce, so the reproduction is asserted.
 *  - every arm: the squeeze row must be present — the longest shipped nav label
 *             (en-XA pseudolocale, 28 chars) against a three-digit count on a
 *             chord-bearing row. Without it the harness only proves the English
 *             label at count 1, the one width nothing squeezes.
 *  - every arm: the app row must render BOTH a run-state mark and a count pill.
 *             That pair is measured against each other rather than against a
 *             chord, because app rows bind none. It overlapped by 64px² in the
 *             pre-fix arm at three digits — the mark sat at a fixed 32px offset
 *             while the pill grew leftward past it — so this is the same class as
 *             the reported bug on a row that reports no shortcut.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-nav-badge-chord.mjs http://127.0.0.1:6812 ../temp-screenshots/nav-badge-chord
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/nav-badge-chord'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 300, height: 260 } })
let failures = 0

for (const fix of ['off', 'on']) {
  for (const theme of ['dark', 'light']) {
    // The before arm only needs one theme: the override is geometry, not colour.
    if (fix === 'off' && theme === 'light') continue
    await page.goto(`${BASE}/capture/nav-badge-chord.html?theme=${theme}&fix=${fix}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-testid="nav-shortcut-chat"]')
    // The hint is opacity-0 until the row is hovered or focused. Hover so the
    // frame shows what the user sees; geometry is unaffected by opacity.
    await page.hover('[aria-keyshortcuts="Alt+C"]')
    const measures = await page.evaluate(() => window.__measure())
    if (measures.length === 0) {
      console.error(`FAIL fix=${fix} theme=${theme}: no right-edge element found — the scene did not seed`)
      failures++
    }
    for (const m of measures) {
      const what = m.against === 'pill' ? 'OVERLAPS COUNT PILL' : 'OVERLAPS CHORD'
      console.log(
        `fix=${fix} theme=${theme} ${m.name}: left=${m.rect.left.toFixed(1)} right=${m.rect.right.toFixed(1)} ` +
        `overlapArea=${m.overlapArea.toFixed(1)} → ${m.overlapsChord ? what : 'clear'}`,
      )
      if (fix === 'on' && m.overlapsChord) {
        console.error(`FAIL: ${m.name} still overlaps the ${m.against === 'pill' ? 'count pill' : 'shortcut hint'} with the fix applied`)
        failures++
      }
      // Zero overlap earned by shoving the chord off the row is not a fix.
      if (fix === 'on' && m.chordClipped) {
        console.error(`FAIL: ${m.name}'s row pushed the shortcut hint outside the row box`)
        failures++
      }
      // Nor is zero overlap earned by clipping the label. Only the Sessions row
      // is checked: the squeeze row's label is the longest translation the app
      // ships and is SUPPOSED to truncate — that is the case it exists to cover.
      if (fix === 'on' && m.labelClipped && m.name.startsWith('sessions/')) {
        console.error(`FAIL: ${m.name}'s row clipped its own label — the right edge outgrew its width budget`)
        failures++
      }
    }
    const badge = measures.find(m => m.name === 'sessions/unread-badge')
    if (fix === 'off' && !badge?.overlapsChord) {
      console.error('FAIL: the pre-fix arm did not reproduce the overlap — before/after evidence would be meaningless')
      failures++
    }
    // The squeeze row (longest shipped label + a three-digit count) must be
    // present in every arm, or "no overlap" is a claim about a scene that never
    // rendered the hard case.
    if (!measures.some(m => m.name === 'squeeze/wide-badge')) {
      console.error(`FAIL fix=${fix} theme=${theme}: the squeeze row did not render — the wide-count case went untested`)
      failures++
    }
    // Same guard for the app row: without it "the run-state mark is clear" would
    // be a claim about a pair that never rendered.
    if (!measures.some(m => m.name === 'app/run-state-vs-pill')) {
      console.error(`FAIL fix=${fix} theme=${theme}: the app row did not render both a run-state mark and a count pill`)
      failures++
    }
    await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${theme}.png` })
  }
}

await browser.close()
if (failures > 0) {
  console.error(`\n${failures} failure(s)`)
  process.exit(1)
}
console.log('\nOK — the badge and the shortcut hint never share a pixel with the fix applied')
