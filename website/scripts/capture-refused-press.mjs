/**
 * Screenshots of the refused-press notice (regenerate / switch-variant).
 *
 * Drives the isolated capture entry (website/capture/refused-press.html), which
 * mounts the REAL ErrorNotice and ChatInput against the real stylesheet, theme
 * tokens, and the live i18n catalog. Not the full SPA: reaching this state in
 * the shell needs a seeded session, a live websocket and a server that refuses,
 * and a half-stubbed shell screenshots its error boundary instead — worse
 * evidence than none.
 *
 * Each scene asserts its RENDERED TEXT before writing the file, so a run can
 * never emit a frame that contradicts the diff (an empty notice, or a title
 * whose catalog key did not resolve).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-refused-press.mjs http://127.0.0.1:6813 ../temp-screenshots/refused-press
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/refused-press'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'before-dark', scene: 'before', theme: 'dark', notice: false, title: '', refusal: '' },
  { name: 'regenerate-dark', scene: 'regenerate', theme: 'dark', notice: true, title: "Couldn't regenerate", refusal: 'slot is running' },
  { name: 'regenerate-light', scene: 'regenerate', theme: 'light', notice: true, title: "Couldn't regenerate", refusal: 'slot is running' },
  { name: 'switch-variant-dark', scene: 'switch_variant', theme: 'dark', notice: true, title: "Couldn't switch version", refusal: 'no variants' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 960, height: 380 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/refused-press.html?scene=${s.scene}&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.waitForSelector('textarea')
  const notice = await page.locator('[data-testid="refused-press-error"]').count()
  const text = notice ? (await page.locator('[data-testid="refused-press-error"]').innerText()).trim() : ''
  const ok = notice === (s.notice ? 1 : 0)
    && (!s.notice || (text.includes(s.title) && text.includes(s.refusal)))
  console.log(`${s.name}: notice=${notice} text=${JSON.stringify(text)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
