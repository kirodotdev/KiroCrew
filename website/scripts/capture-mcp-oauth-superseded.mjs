/**
 * Screenshot + assertion runner for the superseded MCP OAuth banner (issue #7580).
 *
 * The fix's whole point is a NEGATIVE: once a newer authorize request replaces a
 * flow, the older banner must stop offering a link, because that link's loopback
 * listener is gone and clicking it walks the user through a full provider login
 * only to dead-end on `http://127.0.0.1:<dead-port>/?code=...`. A screenshot
 * alone cannot prove an anchor is absent rather than merely off-frame, so this
 * asserts it from the DOM and only then captures the frame:
 *
 *   - the `pending` row DOES expose an <a> (the control still works), and
 *   - the `superseded` row exposes NONE, even though the fixture hands it a
 *     perfectly valid URL on purpose, and
 *   - the superseded row's ring and fill are token-derived. These banners draw
 *     their outline with `ring-1 ring-inset`, i.e. an INSET BOX-SHADOW, and
 *     `border-width` stays 0 outside forced-colors mode -- so reading
 *     `borderTopColor` would report Preflight's grey for every row, passing and
 *     failing for the wrong reason. The rule either emitted a coloured inset
 *     layer or it did not.
 *
 * From website/, with the dev server up:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort
 *   node scripts/capture-mcp-oauth-superseded.mjs http://127.0.0.1:6815 ../temp-screenshots/mcp-oauth-superseded
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/mcp-oauth-superseded'

/** The 1px inset layer Tailwind's `ring-1 ring-inset` emits, with its colour. */
const RING_LAYER = /(rgb|rgba|color)\([^)]*\)\s+0px 0px 0px 1px inset/

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 620, height: 470 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  const name = `${theme}.png`
  try {
    await page.goto(`${BASE}/capture/mcp-oauth-banner.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    await page.waitForSelector('[data-state="superseded"]', { timeout: 10000 })
    await page.waitForTimeout(400)

    const probe = await page.evaluate(() => {
      const row = state => document.querySelector(`[data-state="${state}"]`)
      const styleOf = state => {
        const cs = getComputedStyle(row(state).firstElementChild)
        return { ring: cs.boxShadow, bg: cs.backgroundColor }
      }
      return {
        pendingLinks: row('pending').querySelectorAll('a').length,
        supersededLinks: row('superseded').querySelectorAll('a').length,
        supersededText: row('superseded').textContent.trim(),
        superseded: styleOf('superseded'),
      }
    })

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

    let frameFailed = 0
    if (probe.pendingLinks < 1) {
      frameFailed++
      console.error(`FAIL ${name}: the pending row exposes no <a> -- the live control regressed`)
    }
    if (probe.supersededLinks !== 0) {
      frameFailed++
      console.error(`FAIL ${name}: superseded row still exposes ${probe.supersededLinks} <a> -- a dead loopback link is being offered`)
    }
    if (!/no longer active/i.test(probe.supersededText)) {
      frameFailed++
      console.error(`FAIL ${name}: superseded row does not tell the user the sign-in is dead: "${probe.supersededText}"`)
    }
    const { ring, bg } = probe.superseded
    const ringMatch = ring.match(RING_LAYER)
    if (!ringMatch || /rgba\(0, 0, 0, 0\)/.test(ringMatch[0])) {
      frameFailed++
      console.error(`FAIL ${name}: superseded row emitted no coloured ring (ring-border did not compile), boxShadow=${ring}`)
    }
    if (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') {
      frameFailed++
      console.error(`FAIL ${name}: superseded row emitted no fill (bg-muted/10 did not compile), bg=${bg}`)
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed
    if (!frameFailed) {
      console.log(`ok   ${name}`)
      console.log(`       pending <a>=${probe.pendingLinks}  superseded <a>=${probe.supersededLinks}`)
      console.log(`       superseded ring=${ringMatch[0]}  bg=${bg}`)
      console.log(`       superseded text="${probe.supersededText}"`)
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${name}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed -- the frames do not show the state they claim.`)
  process.exit(1)
}
console.log('\nsuperseded: no link offered, styling emitted, directive present')
