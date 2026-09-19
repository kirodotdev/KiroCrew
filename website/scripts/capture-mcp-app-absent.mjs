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

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

    let frameFailed = 0
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
      console.log(`ok   ${theme}.png -- 1 iframe (live), 2 notices (named + generic fallback), no reopen control, control row bare`)
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
