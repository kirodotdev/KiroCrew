/**
 * Screenshot of the shipped Settings → Display → Terminal "Reuse the current
 * terminal" toggle (issue #11641) — the surface the UX review verifies against
 * the shipped help text.
 *
 * Drives the isolated capture entry
 * (website/capture/display-reuse-current-toggle.html), which mounts the REAL
 * `SettingsToggle` with the SAME label/description props the shipped
 * DisplayPanel passes, read live from the i18n catalog
 * (pages.settings.displayPanel.terminal_reuse_current[/_desc]) against the real
 * stylesheet and theme tokens.
 *
 * The scene asserts the RENDERED description against the catalog AND that it
 * does NOT contain the superseded "opens a new terminal and runs the command"
 * clause before writing, so a run can never emit a frame that contradicts the
 * always-copy behavior the diff ships.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-display-reuse-current-toggle.mjs http://127.0.0.1:6821 ../temp-screenshots/run-in-terminal-copied
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/run-in-terminal-copied'
mkdirSync(OUT, { recursive: true })

// Read the toggle strings from the catalog so a key rename breaks the capture
// loudly instead of silently screenshotting stale copy. The description lives
// in en.manual.json (hand-authored manual overrides).
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const dp = manual.pages?.settings?.displayPanel || {}
const LABEL = dp.terminal_reuse_current
const DESC = dp.terminal_reuse_current_desc
for (const [k, v] of Object.entries({ terminal_reuse_current: LABEL, terminal_reuse_current_desc: DESC })) {
  if (!v) throw new Error(`pages.settings.displayPanel.${k} missing — renamed?`)
}
// Guard against the superseded contract leaking back into the shipped string.
if (/opens a new terminal and runs the command|a fresh terminal opens and runs/i.test(DESC)) {
  throw new Error('terminal_reuse_current_desc still promises a run — the always-copy behavior contradicts it')
}

const SCENES = [
  { name: 'display-reuse-off-dark', theme: 'dark', on: false },
  { name: 'display-reuse-on-dark', theme: 'dark', on: true },
  { name: 'display-reuse-on-light', theme: 'light', on: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 700, height: 220 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/display-reuse-current-toggle.html?theme=${s.theme}${s.on ? '&on=1' : ''}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  const labelOk = await page.getByText(LABEL, { exact: true }).count() > 0
  const descOk = await page.getByText(DESC, { exact: true }).count() > 0
  const ok = labelOk && descOk
  console.log(`${s.name}: label=${labelOk} desc=${descOk} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
  console.log(`wrote ${OUT}/${s.name}.png`)
}

await browser.close()
if (failed) {
  console.error('the toggle did not render the shipped label/description — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
