/**
 * Screenshots of the Issue Radar copy-link confirmation.
 *
 * Drives the isolated capture entry (website/capture/copy-link-feedback.html),
 * which mounts the REAL IssueDetail pane inside the REAL IssueRadarProvider
 * against the real stylesheet, theme tokens and live i18n catalog. Not the full
 * SPA: reaching this row in the shell needs a connected provider repo and a live
 * issue, and a half-stubbed shell screenshots its error boundary instead.
 *
 * Each scene asserts the row's RENDERED TEXT before writing the file, so a run
 * can never emit a frame that contradicts the diff (a tick claiming a copy that
 * did not happen, or a catalog key that did not resolve).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-copy-link-feedback.mjs http://127.0.0.1:6821 ../temp-screenshots/copy-link-feedback
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/copy-link-feedback'
mkdirSync(OUT, { recursive: true })

// The secure-context success path is pinned by the pane's unit tests and renders
// the same frame as `fallback`, so it is not screenshotted twice here.
const SCENES = [
  { name: 'idle-dark', scene: 'idle', theme: 'dark', press: false, expect: 'Copy link to this issue' },
  { name: 'copied-plain-http-dark', scene: 'fallback', theme: 'dark', press: true, expect: 'Link copied' },
  { name: 'failed-dark', scene: 'failed', theme: 'dark', press: true, expect: 'Copy failed' },
  { name: 'failed-light', scene: 'failed', theme: 'light', press: true, expect: 'Copy failed' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1060, height: 580 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/copy-link-feedback.html?scene=${s.scene}&theme=${s.theme}`)
  // The menu fades and zooms in on open. A frame taken mid-animation shows the
  // page bleeding through the translucent panel, which reads as a rendering
  // defect rather than as the state under test, so every animation is collapsed
  // to its settled end state before anything is captured.
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  await page.waitForSelector('h1')
  await page.getByRole('button', { name: 'More actions' }).click()
  const row = page.getByRole('menuitem', { name: /copy link to this issue|link copied|copy failed/i })
  await row.waitFor()
  if (s.press) {
    // The row keeps the menu open on select, so the confirmation is readable
    // where it was earned. It also reverts on a timer, so the frame is taken
    // inside that window.
    await row.click()
    await page.getByRole('menuitem', { name: s.expect }).waitFor({ timeout: 2000 })
  }
  const text = (await row.innerText()).trim()
  const ok = text === s.expect
  console.log(`${s.name}: row=${JSON.stringify(text)} ${ok ? 'OK' : `MISMATCH (wanted ${JSON.stringify(s.expect)})`}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
