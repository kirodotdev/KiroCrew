/**
 * Screenshots of the chat approval bar's Reject dropdown and its two tiers (#6068).
 *
 * Reuses the existing isolated capture entry
 * (website/capture/approval-mode-discover.html), which mounts the REAL
 * ChatInput against the real stylesheet, theme tokens and live i18n catalog
 * with an unresolved permission message seeded into the real store — exactly
 * the state this PR changes.
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document the wrong state:
 *   after (default): the row carries ONE "Reject" trigger (the row is at its
 *     count AUTOSDE's max-two-buttons-per-row exempts as pre-existing, so a
 *     second rejection button would breach it), and opening it reveals BOTH tiers.
 *     This assertion FAILS on the pre-fix code, so an after-frame cannot be
 *     shot from the old component by mistake.
 *   --before: the "Reject" control is a plain BUTTON with no dropdown
 *     chevron and no menu — the cascade-only state this PR replaces
 *     (pre-fix checkout).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-reject-once-approval.mjs http://127.0.0.1:6824 ../temp-screenshots/reject-once-6068 [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/reject-once-6068'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'reject-once-approval-bar-dark', theme: 'dark' },
  { name: 'reject-once-approval-bar-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 380 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/approval-mode-discover.html?scene=hint&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.locator('[data-capture-root] button', { hasText: 'Allow once' }).first().waitFor()

  const rowLabels = (
    await page.locator('[data-capture-root] button').allInnerTexts()
  ).map(t => t.trim())
  const rejectControls = rowLabels.filter(l => l === 'Reject').length
  const hasMenu = async () => (await page.getByRole('menuitem').count()) > 0

  // Read this BEFORE opening anything: Radix aria-hides outside content while a
  // menu is up, which takes the trigger out of the accessibility tree.
  const popup = await page
    .getByRole('button', { name: 'Reject', exact: true })
    .getAttribute('aria-haspopup')

  if (BEFORE) {
    // Discriminate WITHOUT touching the control: a Radix trigger sets
    // aria-haspopup, a plain button does not. Hovering (or clicking) would put
    // the before-frame in a different visual state than the after-frame, so the
    // pair would read as if Reject had lost its tint — comparing resting states
    // is the whole point of a before/after pair.
    const popup = await page
      .getByRole('button', { name: 'Reject', exact: true })
      .getAttribute('aria-haspopup')
    const ok = rejectControls === 1 && !popup && !(await hasMenu())
    console.log(`${s.name} (before): row=${JSON.stringify(rowLabels)} ${ok ? 'OK' : 'MISMATCH'}`)
    if (!ok) { failed = true; continue }
    await page.screenshot({ path: `${OUT}/${s.name}-before.png` })
    continue
  }

  // Open the REAL dropdown rather than forcing component state, so the frame
  // documents the shipped wiring.
  await page.getByRole('button', { name: 'Reject', exact: true }).click()
  const menu = page.getByRole('menu')
  await menu.waitFor()
  // Radix opens with a fade-in; a frame shot mid-animation reads as a
  // low-contrast rendering defect the menu does not have. Wait for the entry
  // animation to settle AND assert full opacity, so a frame cannot document it.
  await page.waitForFunction(() => {
    const el = document.querySelector('[role="menu"]')
    return !!el && getComputedStyle(el).opacity === '1'
  })
  const items = (await page.getByRole('menuitem').allInnerTexts()).map(t => t.trim())
  const ok =
    rejectControls === 1 &&
    !!popup &&
    rowLabels.includes('Allow once') &&
    items.some(i => i.startsWith('Reject once')) &&
    items.some(i => i.startsWith('Reject all'))
  console.log(
    `${s.name}: row=${JSON.stringify(rowLabels)} menu=${JSON.stringify(items)} ${ok ? 'OK' : 'MISMATCH'}`,
  )
  if (!ok) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('capture failed: at least one frame did not match its asserted state')
  process.exit(1)
}
