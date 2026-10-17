/**
 * Screenshot of the reuse-path COPY-VARIANT confirm dialog (RunInTerminalConfirm).
 *
 * Drives the isolated capture entry
 * (website/capture/run-in-terminal-confirm-copy.html), which mounts the REAL
 * RunInTerminalConfirm with `open` and `willCopy` true against the real
 * stylesheet, theme tokens and live i18n catalog. The dialog therefore renders
 * the shipped copy-variant strings:
 *   - title  components.runInTerminalConfirm.title_copy  ("Copy to terminal")
 *   - body   components.runInTerminalConfirm.body_copy
 *   - button components.runInTerminalConfirm.copy         ("Copy")
 *
 * The scene asserts the RENDERED primary button text and title against the
 * catalog before writing, so a run can never emit a frame that contradicts the
 * diff (a stale Run label or an unresolved catalog key).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-run-in-terminal-confirm-copy.mjs http://127.0.0.1:6821 ../temp-screenshots/run-in-terminal-copied
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/run-in-terminal-copied'
mkdirSync(OUT, { recursive: true })

// Read the copy-variant strings from the catalog so a key rename breaks the
// capture loudly instead of silently screenshotting stale copy.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const en = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const c = en.components?.runInTerminalConfirm || {}
const TITLE_COPY = c.title_copy
const BODY_COPY = c.body_copy
const COPY = c.copy
for (const [k, v] of Object.entries({ title_copy: TITLE_COPY, body_copy: BODY_COPY, copy: COPY })) {
  if (!v) throw new Error(`components.runInTerminalConfirm.${k} missing — renamed?`)
}

const SCENES = [
  { name: 'confirm-copy-dark', theme: 'dark' },
  { name: 'confirm-copy-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 680, height: 340 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/run-in-terminal-confirm-copy.html?theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  // The dialog is open from mount (open + willCopy). Assert the copy-variant
  // primary button and title before shooting so the frame proves the ship.
  const btn = page.getByRole('button', { name: new RegExp(`^${COPY}$`) })
  await btn.waitFor({ timeout: 2000 })
  const btnText = (await btn.textContent())?.trim()
  const titleOk = await page.getByText(TITLE_COPY, { exact: true }).count() > 0
  const bodyOk = await page.getByText(BODY_COPY, { exact: true }).count() > 0
  const ok = btnText === COPY && titleOk && bodyOk
  console.log(`${s.name}: button=${JSON.stringify(btnText)} title=${titleOk} body=${bodyOk} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
  console.log(`wrote ${OUT}/${s.name}.png`)
}

await browser.close()
if (failed) {
  console.error('the copy-variant dialog did not render the shipped copy — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
