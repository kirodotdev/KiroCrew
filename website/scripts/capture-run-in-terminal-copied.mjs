/**
 * Screenshot of the reuse-path "Copied" flash on RunInTerminalBtn.
 *
 * Drives the isolated capture entry (website/capture/run-in-terminal-copied.html),
 * which mounts the REAL RunInTerminalBtn against the real stylesheet, theme
 * tokens and live i18n catalog. The copied status is reached through the
 * component's own event contract: the entry answers the button's
 * `mc:run-in-terminal` request with `{ ok: true, copied: true }`, exactly as
 * ChatPage does when dashboard.terminal.reuse_current is on and an existing tab
 * is focused — so the glyph and its reuse-specific string
 * (components.runInTerminalBtn.copied_paste_into_terminal) are the shipped ones.
 *
 * The scene asserts the RENDERED aria-label before writing, so a run can never
 * emit a frame that contradicts the diff (the generic markdown "Copied!" or an
 * unresolved catalog key). The flash reverts after 1200ms; the frame is taken
 * inside that window.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-run-in-terminal-copied.mjs http://127.0.0.1:6821 ../temp-screenshots/run-in-terminal-copied
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/run-in-terminal-copied'
mkdirSync(OUT, { recursive: true })

// Read the label from the catalog so a key rename breaks the capture loudly
// instead of silently screenshotting the wrong glyph.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const LABEL = manual.components?.runInTerminalBtn?.copied_paste_into_terminal
if (!LABEL) throw new Error('components.runInTerminalBtn.copied_paste_into_terminal missing — renamed?')

const RUN = manual.components?.runInTerminalConfirm?.run || 'Run'

const SCENES = [
  { name: 'copied-dark', theme: 'dark' },
  { name: 'copied-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 400, height: 200 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/run-in-terminal-copied.html?theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  // Open the confirm dialog, then confirm — the reuse path only copies, but the
  // click still routes through the confirmation the button always shows.
  await page.getByRole('button', { name: /run in terminal/i }).click()
  await page.getByRole('button', { name: new RegExp(`^${RUN}$`, 'i') }).click()
  // The confirm dialog unmounts on confirm; wait for it to fully detach so it
  // does not overlay the flash in the frame.
  await page.getByRole('button', { name: new RegExp(`^${RUN}$`, 'i') }).waitFor({ state: 'detached', timeout: 2000 })
  // The copied flash is a status glyph carrying the reuse-specific aria-label.
  const glyph = page.getByLabel(LABEL)
  await glyph.waitFor({ timeout: 2000 })
  const rendered = await glyph.getAttribute('aria-label')
  const ok = rendered === LABEL
  console.log(`${s.name}: aria-label=${JSON.stringify(rendered)} ${ok ? 'OK' : `MISMATCH (wanted ${JSON.stringify(LABEL)})`}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
  console.log(`wrote ${OUT}/${s.name}.png`)
}

await browser.close()
if (failed) {
  console.error('the copied glyph did not render the reuse-specific label — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
