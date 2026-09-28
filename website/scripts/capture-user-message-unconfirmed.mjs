/**
 * Screenshots of the user chat bubble's unconfirmed state — before vs after.
 *
 * Drives the ISOLATED capture entry (website/capture/user-message-unconfirmed.html),
 * which mounts the real UserMessage against the real stylesheet and theme tokens.
 * The state being photographed was reachable in the running app only ~30-40s after
 * a send whose server echo never arrived, so shooting it live means stalling the
 * WebSocket for half a minute; handing the component the exact meta the reducer
 * used to write reaches the same render with no gateway and no timing.
 *
 * The run is SELF-CHECKING, and that is the whole point of the pair: it counts the
 * indicator's `role="status"` node and FAILS unless the count matches the `expect`
 * argument. So the "before" shot cannot be taken from a checkout that no longer
 * draws the indicator, and the "after" shot cannot be taken from one that still
 * does — a mislabelled pair exits non-zero instead of emitting a misleading image.
 *
 * Usage (one shell for the server, one per checkout for the shot):
 *   npx vite --host 127.0.0.1 --port 5599 --strictPort
 *   node scripts/capture-user-message-unconfirmed.mjs http://127.0.0.1:5599 \
 *     ../temp-screenshots/unconfirmed-indicator after absent
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5599'
const OUT = process.argv[3] || '../temp-screenshots/unconfirmed-indicator'
const LABEL = process.argv[4] || 'after'
const EXPECT = process.argv[5] || 'absent'
mkdirSync(OUT, { recursive: true })

if (!['present', 'absent'].includes(EXPECT)) {
  console.error(`expect must be 'present' or 'absent', got ${EXPECT}`)
  process.exit(2)
}
const wantCount = EXPECT === 'present' ? 1 : 0

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 760, height: 460 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${BASE}/capture/user-message-unconfirmed.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    try {
      // Three bubbles must exist before anything is asserted about the notice —
      // an empty root would otherwise satisfy `expect=absent` vacuously.
      await page.waitForFunction(
        () => document.querySelectorAll('[data-capture-root] .message-bubble').length >= 3,
        { timeout: 15000 },
      )
    } catch {
      console.error(
        `  FAIL ${theme}: fewer than 3 bubbles rendered` +
          (errors.length ? ` (${errors[0]})` : ''),
      )
      failed += 1
      await ctx.close()
      continue
    }

    const seen = await page.$$eval('[data-capture-root] [role="status"]', (nodes) =>
      nodes.map((n) => n.textContent?.trim() || ''),
    )
    if (seen.length !== wantCount) {
      console.error(
        `  FAIL ${theme}: expected ${wantCount} unconfirmed notice(s) for ` +
          `expect=${EXPECT}, saw ${seen.length}` +
          (seen.length ? ` (${JSON.stringify(seen)})` : ''),
      )
      failed += 1
      await ctx.close()
      continue
    }

    const file = `${OUT}/${LABEL}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path: file })
    console.log(`  ok   ${theme} ${LABEL} (${EXPECT}) -> ${file}`)
    await ctx.close()
  }
  await browser.close()
  if (failed) {
    console.error(`\n${failed} capture(s) failed`)
    process.exit(1)
  }
}

run()
