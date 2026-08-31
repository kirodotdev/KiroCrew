/**
 * Real-layout measurement + screenshots for the `(recommended)` option badge.
 *
 * Drives the isolated capture entry (website/capture/followup-recommended-badge.html),
 * which mounts the REAL FollowUpBar inside ChatInput's `input-area` box chain and
 * exposes window.__measure().
 *
 * Assertions, per theme:
 *  - fix=off: the long label IS clipped and the marker is NOT painted. This is the
 *    before arm and it must reproduce; a before frame that already shows the marker
 *    would mean the harness is not rendering upstream's shape and every after-arm
 *    claim below would be unearned.
 *  - fix=on: the badge is present and painted inside the chip, on the SAME label
 *    that is still clipped — so the marker survives clamping rather than the label
 *    having simply got shorter.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-followup-recommended-badge.mjs http://127.0.0.1:6813 ../temp-screenshots/followup-recommended-badge
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/followup-recommended-badge'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1280, height: 320 }
/** The narrowest content width, so the clamp actually bites. */
const WIDTH = 'compact'
/** The staggered chip entrance is still translating for ~750ms after mount. */
const ENTRANCE_SETTLE_MS = 900

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const check = (label, ok, detail) => {
  if (!ok) failures++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
}

for (const theme of ['dark', 'light']) {
  for (const fix of ['off', 'on']) {
    const page = await browser.newPage({ viewport: VIEWPORT, colorScheme: theme })
    await page.goto(`${BASE}/capture/followup-recommended-badge.html?width=${WIDTH}&theme=${theme}&fix=${fix}`)
    await page.waitForSelector('[data-bar] .truncate')
    await page.waitForTimeout(ENTRANCE_SETTLE_MS)

    const m = await page.evaluate(() => window.__measure())
    const tag = `${theme}/fix=${fix}`

    // Shared precondition: without a clipped label neither arm means anything.
    check(`${tag} long label is clipped`, m.labelClipped, `scroll>client=${m.labelClipped}`)

    if (fix === 'off') {
      // The defect is NOT that the marker is invisible -- a leading marker is never
      // what the ellipsis eats. It is that the marker is inside the label, and the
      // label is what a click sends as the user's own message.
      check(`${tag} marker is part of the dispatched label (defect reproduces)`, m.markerInLabel === true, `markerInLabel=${m.markerInLabel}`)
      check(`${tag} no badge element exists`, m.badgeCount === 0, `badgeCount=${m.badgeCount}`)
    } else {
      check(`${tag} badge painted inside the chip`, m.markerVisible === true, `markerVisible=${m.markerVisible}`)
      check(`${tag} the marked option is badged`, m.badgeCount === 1, `badgeCount=${m.badgeCount}`)
      check(`${tag} the label no longer carries the marker`, m.markerInLabel === false, `markerInLabel=${m.markerInLabel}`)
    }

    const name = `${fix === 'off' ? '01-before' : '02-after'}-${theme}.png`
    await page.screenshot({ path: `${OUT}/${name}` })
    console.log(`     shot ${OUT}/${name}`)
    await page.close()
  }
}

await browser.close()
console.log(failures ? `\n${failures} assertion(s) FAILED` : '\nall assertions passed')
process.exit(failures ? 1 : 0)
