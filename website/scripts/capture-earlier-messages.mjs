/**
 * Screenshots of the top-of-transcript earlier-messages affordance.
 *
 * Drives the isolated capture entry (website/capture/earlier-messages.html),
 * which mounts the real component against the real stylesheet and theme tokens.
 *
 * Every scene ASSERTS before it shoots: the label must be non-empty (an
 * uninitialised i18next renders an empty button, which would look like a styling
 * bug rather than a missing catalog), and the disabled/aria-busy pair must match
 * the scene. A screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-earlier-messages.mjs http://127.0.0.1:6811 ../temp-screenshots/earlier-messages
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/earlier-messages'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { scene: 'idle', busy: 'false', note: 'unloaded history advertised; control actionable' },
  { scene: 'loading', busy: 'true', note: 'fetch in flight; control inert but still focusable' },
  { scene: 'failed', busy: 'false', note: 'fetch rejected; failure named and retry available' },
]

/** Rows the entry mounts under the marker; fewer means the mount broke. */
const MIN_TRANSCRIPT_ROWS = 3

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, busy, note } of SCENES) {
      const ctx = await browser.newContext({ viewport: { width: 700, height: 480 }, deviceScaleFactor: 1 })
      const page = await ctx.newPage()
      await page.goto(`${BASE}/capture/earlier-messages.html?scene=${scene}&theme=${theme}`)
      const btn = await page.waitForSelector('[data-testid="load-earlier-messages"]', { timeout: 10_000 })

      // The point of the frame is the marker's position ABOVE a transcript, so a
      // failed message mount must fail the run rather than reshoot a bare control.
      const rows = await page.$$('[data-testid="capture-transcript"] > *')
      if (rows.length < MIN_TRANSCRIPT_ROWS) {
        console.error(`  FAIL ${theme}/${scene}: ${rows.length} transcript row(s), `
          + `expected >= ${MIN_TRANSCRIPT_ROWS} — the frame would show the control with no context`)
        failed += 1
        await ctx.close()
        continue
      }

      const label = (await btn.textContent())?.trim() ?? ''
      if (!label) {
        console.error(`  FAIL ${theme}/${scene}: button label is empty (i18next not initialised?)`)
        failed += 1
        await ctx.close()
        continue
      }
      const actualBusy = await btn.getAttribute('aria-busy')
      const ariaDisabled = await btn.getAttribute('aria-disabled')
      // native-disabled must stay false in EVERY scene: `disabled` is what drops
      // focus to <body>, which strands the keyboard users this control is for.
      // Read the DOM property, not isDisabled() — that also reports aria-disabled,
      // so it returns true either way and cannot tell the two apart.
      const nativelyDisabled = await btn.evaluate((el) => el.disabled)
      if (actualBusy !== busy || ariaDisabled !== busy || nativelyDisabled) {
        console.error(`  FAIL ${theme}/${scene}: expected aria-busy=${busy} aria-disabled=${busy} `
          + `native-disabled=false, saw aria-busy=${actualBusy} aria-disabled=${ariaDisabled} `
          + `native-disabled=${nativelyDisabled}`)
        failed += 1
        await ctx.close()
        continue
      }

      const target = await page.$('[data-capture-root]')
      await target.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
      console.log(`  ${theme}/${scene} -> "${label}" — ${note}`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} scene(s) failed`)
    process.exit(1)
  }
}

run()
