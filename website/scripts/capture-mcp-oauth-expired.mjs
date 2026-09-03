/**
 * Screenshot + assertion runner for the EXPIRED MCP OAuth banner (issue #7654).
 *
 * Sibling of capture-mcp-oauth-superseded.mjs, and deliberately a separate lane
 * because the two states differ in what they must SAY, not in how they look. Both
 * withdraw the link; only this one has no newer flow to point at.
 *
 * A screenshot cannot prove an anchor is absent rather than off-frame, and it
 * cannot prove a sentence is absent at all, so both are asserted from the DOM
 * before the frame is captured:
 *
 *   - the `pending` row DOES expose an <a> (the live control still works), and
 *   - the `expired` row exposes NONE, even though the fixture hands it a valid
 *     URL on purpose, and
 *   - the expired copy says the sign-in is no longer active, and
 *   - the expired copy does NOT say "newer request" or "latest Authorize button".
 *     That is the whole reason this is not just reusing `superseded`: after a
 *     restart or a reset nothing replaced the flow, so that advice would be false
 *     and would send the user looking for a button that does not exist.
 *   - the expired copy names a reachable recovery ("send a message"), so the user
 *     is not left with a dead end -- hiding the banner outright would strand them
 *     AND read as success.
 *   - the row's ring and fill are token-derived. These banners draw their outline
 *     with `ring-1 ring-inset`, an INSET BOX-SHADOW, and `border-width` stays 0
 *     outside forced-colors mode, so reading `borderTopColor` would report
 *     Preflight's grey for every row and pass or fail for the wrong reason.
 *
 * From website/, with the dev server up:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort
 *   node scripts/capture-mcp-oauth-expired.mjs http://127.0.0.1:6815 ../temp-screenshots/mcp-oauth-expired
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/mcp-oauth-expired'

/** The 1px inset layer Tailwind's `ring-1 ring-inset` emits, with its colour. */
const RING_LAYER = /(rgb|rgba|color)\([^)]*\)\s+0px 0px 0px 1px inset/

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    // Five rows now, so taller than the superseded runner's frame.
    viewport: { width: 620, height: 560 },
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
    await page.waitForSelector('[data-state="expired"]', { timeout: 10000 })
    await page.waitForTimeout(400)

    const probe = await page.evaluate(() => {
      const row = state => document.querySelector(`[data-state="${state}"]`)
      const cs = getComputedStyle(row('expired').firstElementChild)
      return {
        pendingLinks: row('pending').querySelectorAll('a').length,
        expiredLinks: row('expired').querySelectorAll('a').length,
        expiredText: row('expired').textContent.trim(),
        supersededText: row('superseded').textContent.trim(),
        ring: cs.boxShadow,
        bg: cs.backgroundColor,
      }
    })

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

    let frameFailed = 0
    const fail = msg => { frameFailed++; console.error(`FAIL ${name}: ${msg}`) }

    if (probe.pendingLinks < 1) {
      fail('the pending row exposes no <a> -- the live control regressed')
    }
    if (probe.expiredLinks !== 0) {
      fail(`expired row still exposes ${probe.expiredLinks} <a> -- a dead loopback link is being offered`)
    }
    if (!/no longer active/i.test(probe.expiredText)) {
      fail(`expired row does not tell the user the sign-in is dead: "${probe.expiredText}"`)
    }
    if (/newer request/i.test(probe.expiredText)) {
      fail(`expired row claims a newer request replaced the flow, which is false here: "${probe.expiredText}"`)
    }
    if (/latest Authorize button/i.test(probe.expiredText)) {
      fail(`expired row points at a "latest Authorize button" that does not exist: "${probe.expiredText}"`)
    }
    if (!/send a message/i.test(probe.expiredText)) {
      fail(`expired row names no reachable recovery, stranding the user: "${probe.expiredText}"`)
    }
    // Guard the pairing itself: if both states ever render identical copy, this
    // lane and the superseded lane would both pass while the distinction is gone.
    if (probe.expiredText === probe.supersededText) {
      fail('expired and superseded render identical copy -- the states are no longer distinguishable')
    }
    const ringMatch = probe.ring.match(RING_LAYER)
    if (!ringMatch || /rgba\(0, 0, 0, 0\)/.test(ringMatch[0])) {
      fail(`expired row emitted no coloured ring (ring-border did not compile), boxShadow=${probe.ring}`)
    }
    if (probe.bg === 'rgba(0, 0, 0, 0)' || probe.bg === 'transparent') {
      fail(`expired row emitted no fill (bg-muted/10 did not compile), bg=${probe.bg}`)
    }
    if (errors.length) {
      fail(`${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }

    failed += frameFailed
    if (!frameFailed) {
      console.log(`ok   ${name}`)
      console.log(`       pending <a>=${probe.pendingLinks}  expired <a>=${probe.expiredLinks}`)
      console.log(`       expired ring=${ringMatch[0]}  bg=${probe.bg}`)
      console.log(`       expired text="${probe.expiredText}"`)
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
console.log('\nexpired: no link offered, honest cause, reachable recovery, styling emitted')
