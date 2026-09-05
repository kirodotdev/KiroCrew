/**
 * Screenshots of the user bubble's UNKNOWN-DELIVERY state, dark and light.
 *
 * Drives capture/user-message-delivery-unknown.html, which mounts the real
 * UserMessage against the real stylesheet and theme tokens. The state needs a
 * transport failure to reach in the running app, so the harness hands the
 * component the exact meta the reducer writes instead of breaking the network.
 *
 * SELF-CHECKING, so a misleading image cannot be emitted: it requires all three
 * bubbles to render (an empty root would satisfy any count vacuously) and then
 * requires EXACTLY ONE `role="status"` caption — the middle row's. A checkout
 * that lost the caption, or one that captioned every row, exits non-zero.
 *
 * Usage (one shell for the server, one for the shot):
 *   npx vite --host 127.0.0.1 --port 5599 --strictPort
 *   node scripts/capture-user-message-delivery-unknown.mjs \
 *     http://127.0.0.1:5599 ../temp-screenshots/optimistic-bubble-delivery
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5599'
const OUT = process.argv[3] || '../temp-screenshots/optimistic-bubble-delivery'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 760, height: 520 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${BASE}/capture/user-message-delivery-unknown.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    try {
      await page.waitForFunction(
        () => document.querySelectorAll('[data-capture-root] .message-bubble').length >= 3,
        { timeout: 15000 },
      )
    } catch {
      console.error(`  FAIL ${theme}: fewer than 3 bubbles rendered` + (errors.length ? ` (${errors[0]})` : ''))
      failed += 1
      await ctx.close()
      continue
    }

    // Selected by a stable capture hook, NOT `role="status"`: the caption deliberately is not
    // a live region, because the composer echo is the one that announces this string.
    const captions = await page.$$eval('[data-capture-root] [data-delivery-caption]', (nodes) =>
      nodes.map((n) => n.textContent?.trim() || ''),
    )
    if (captions.length !== 1) {
      console.error(`  FAIL ${theme}: expected exactly 1 unconfirmed caption, saw ${captions.length} ${JSON.stringify(captions)}`)
      failed += 1
      await ctx.close()
      continue
    }

    const file = `${OUT}/delivery-unknown-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path: file })
    console.log(`  ok   ${theme} -> ${file} (caption: ${JSON.stringify(captions[0])})`)
    await ctx.close()
  }
  await browser.close()
  if (failed) {
    console.error(`\n${failed} capture(s) failed`)
    process.exit(1)
  }
}

run()
