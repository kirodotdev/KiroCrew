/**
 * Screenshot + assertion runner for capture/mcp-app-absent.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort
 *   node scripts/capture-mcp-app-absent.mjs http://127.0.0.1:6821 \
 *     ../temp-screenshots/mcp-app-absent
 *
 * The assertions matter more than the image. All three app rows carry the same
 * persisted flag and differ only by whether a live payload exists for their id,
 * so a fixture that seeded the payload under the wrong key would render three
 * notices, or three iframes, and the frame would still look plausible. The probe
 * checks one iframe (the live row), two notices (the reloaded row and the
 * side-panel one), no reopen control (no tab survives a reload), and that the
 * control row grew neither.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/mcp-app-absent'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 620 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  try {
    await page.goto(`${BASE}/capture/mcp-app-absent.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    // The row reveal animation and the iframe's first paint need to settle.
    await page.waitForTimeout(1200)

    const seen = await page.evaluate(() => {
      const root = document.querySelector('[data-capture-root]')
      const text = root?.textContent || ''
      return {
        iframes: root ? root.querySelectorAll('iframe').length : 0,
        notices: (text.match(/not viewable here/g) || []).length,
        askAgain: /Ask the agent to show it again/.test(text),
        reopen: (text.match(/Opened in the side panel/g) || []).length,
        control: (root?.querySelector('[data-row="t_plain"]')?.textContent || '').trim(),
        // Per row, so the two notice branches are told apart rather than
        // counted together: the reloaded row carries an mcp_server and must
        // NAME it, the side-panel row carries none and must use the fallback.
        named: (root?.querySelector('[data-row="t_gone"]')?.textContent || '').trim(),
        fallback: (root?.querySelector('[data-row="t_panel"]')?.textContent || '').trim(),
      }
    })

    let frameFailed = 0
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

    // THE LIVE SWAP, which no image showed before: the same row photographed with
    // its frame and then with its notice, the eviction driven through the shipped
    // reducer rather than by deleting the payload for the camera. Review's
    // remaining gate on this PR was that nobody had watched this happen.
    const evictedAfter = await page.evaluate(() => window.__kcEvictLiveApp?.() ?? -1)
    await page.waitForTimeout(600)
    const after = await page.evaluate(() => {
      const root = document.querySelector('[data-capture-root]')
      const text = root?.textContent || ''
      return {
        iframes: root ? root.querySelectorAll('iframe').length : 0,
        notices: (text.match(/not viewable here/g) || []).length,
        live: (root?.querySelector('[data-row="t_live"]')?.textContent || '').trim(),
      }
    })
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-after-eviction.png` })

    if (evictedAfter < 0) {
      frameFailed++
      console.error(`FAIL ${theme}: the live payload outlived the eviction driver, so the after-shot is not the swap`)
    }
    if (after.iframes !== 0) {
      frameFailed++
      console.error(`FAIL ${theme}: ${after.iframes} iframe(s) after eviction, expected 0 -- the frame must be gone`)
    }
    if (after.notices !== 3) {
      frameFailed++
      console.error(`FAIL ${theme}: ${after.notices} notice(s) after eviction, expected 3 -- the evicted row must gain one`)
    }
    if (!/not viewable here/.test(after.live)) {
      frameFailed++
      console.error(`FAIL ${theme}: the evicted row itself carries no notice, so the live patch path is unphotographed`)
    }
    // The evicted row must NAME its app, not fall to the generic wording. That branch
    // is only reachable after an eviction, so nothing exercised it until the swap was
    // driven, and a row seeded without `mcp_server` passed every other assertion here
    // while showing a notice the live path does not produce.
    if (!/The excalidraw app from this step/.test(after.live)) {
      frameFailed++
      console.error(`FAIL ${theme}: the evicted row does not name its server, so the after-shot shows a notice the live path never draws`)
    }

    if (seen.iframes !== 1) {
      frameFailed++
      console.error(`FAIL ${theme}: ${seen.iframes} iframe(s), expected exactly 1 (the live row only)`)
    }
    if (seen.notices !== 2) {
      frameFailed++
      console.error(`FAIL ${theme}: ${seen.notices} notice(s), expected exactly 2 (the reloaded row and the side-panel one)`)
    }
    if (seen.reopen !== 0) {
      frameFailed++
      console.error(`FAIL ${theme}: a reopen control rendered, but no app tab survives a reload`)
    }
    if (/viewable/.test(seen.control)) {
      frameFailed++
      console.error(`FAIL ${theme}: the control row grew a notice, so the flag is not what gates it`)
    }
    if (!seen.askAgain) {
      frameFailed++
      console.error(`FAIL ${theme}: the notice does not carry the way back ("Ask the agent to show it again")`)
    }
    if (!/The excalidraw app from this step/.test(seen.named)) {
      frameFailed++
      console.error(`FAIL ${theme}: the reloaded row does not NAME its server, so the named notice is unphotographed`)
    }
    if (!/An app from this step is not viewable here/.test(seen.fallback.replace(/\s+/g, ' '))) {
      frameFailed++
      console.error(`FAIL ${theme}: the row with no server identity does not use the generic fallback`)
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${theme}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed

    if (!frameFailed) {
      console.log(`ok   ${theme}.png -- 1 iframe (live), 2 notices (named + generic fallback), no reopen control, control row bare; ${theme}-after-eviction.png -- the same row after the live patch: 0 iframes, 3 notices, evicted in ${evictedAfter} render(s)`)
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${theme}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed -- the frames do not show the states they claim.`)
  process.exit(1)
}
