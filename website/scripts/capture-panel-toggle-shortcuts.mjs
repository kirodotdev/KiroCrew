/**
 * Screenshots for the rebindable panel-toggle shortcuts (PR #4490, #4488).
 *
 * Drives website/capture/panel-toggle-shortcuts.html. Four shots:
 *   1. settings/dark        — Settings → Shortcuts with the new "Panel toggles"
 *                             section at factory defaults (sidebar "Not set",
 *                             session Ctrl/Cmd+B, side panel Ctrl/Cmd+\).
 *   2. settings/light       — same, light theme.
 *   3. settings-custom/dark — a recorded custom chord + a cleared toggle, so
 *                             the record/clear affordances are shown live.
 *   4. modal/dark           — the Alt+K modal listing the panel toggles.
 *
 * Each scene ASSERTS the rendered "Panel toggles" heading (and per-scene
 * binding text) before writing the file, so a frame cannot silently
 * photograph the wrong state.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-panel-toggle-shortcuts.mjs http://127.0.0.1:6841 ../temp-screenshots/panel-toggle-shortcuts
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/panel-toggle-shortcuts'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 720 }, deviceScaleFactor: 2 })

let failed = false

async function shoot(scene, theme, name, assertText, opts = {}) {
  await page.goto(`${BASE}/capture/panel-toggle-shortcuts.html?scene=${scene}&theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  if (opts.scrollTo) {
    await page.getByText(opts.scrollTo, { exact: true }).first().scrollIntoViewIfNeeded()
  }
  // Entrance animations settle before measuring/shooting.
  await page.waitForTimeout(900)
  for (const text of assertText) {
    const hit = await page.getByText(text, { exact: false }).first().isVisible().catch(() => false)
    if (!hit) {
      console.error(`FAIL [${name}]: expected visible text ${JSON.stringify(text)}`)
      failed = true
    }
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: !opts.scrollTo })
  console.log(`wrote ${OUT}/${name}.png`)
}

// Settings: an unbound toggle renders the "Press keys…" recorder affordance;
// bound rows render the chord plus a Clear button.
await shoot('settings', 'dark', 'settings-defaults-dark', ['Panel toggles', 'Toggle session panel', 'Press keys'])
await shoot('settings', 'light', 'settings-defaults-light', ['Panel toggles', 'Toggle side panel'])
await shoot('settings-custom', 'dark', 'settings-custom-dark', ['Panel toggles', 'Press keys'], { scrollTo: 'Panel toggles' })
// Modal: "Not set" is the modal's unbound string; the section sits below the
// modal's internal scroll fold, so bring it into view and shoot the viewport.
await shoot('modal', 'dark', 'modal-dark', ['Panel toggles', 'Not set'], { scrollTo: 'Panel toggles' })

await browser.close()
process.exit(failed ? 1 : 0)
