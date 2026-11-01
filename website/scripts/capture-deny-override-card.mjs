/**
 * Screenshots of the security-rule override card.
 *
 * Uses the isolated capture entry website/capture/deny-override-card.html, which
 * mounts the REAL ChatInput with the permission row the gateway emits when a
 * deny rule refused a command and a person may override it.
 *
 * Each frame ASSERTS its state before it is written: the warning names the rule,
 * the bar has no "Allow once" button, no "Trust" control and no approval-mode
 * hint, and the open menu
 * lists exactly the two override scopes.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort   # in another shell
 *   node scripts/capture-deny-override-card.mjs http://127.0.0.1:6825 ../temp-screenshots/deny-override
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/deny-override'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'deny-override-dark', theme: 'dark', lang: 'en', open: false },
  { name: 'deny-override-dark-menu', theme: 'dark', lang: 'en', open: true },
  { name: 'deny-override-light-menu', theme: 'light', lang: 'en', open: true },
  { name: 'deny-override-dark-zh', theme: 'dark', lang: 'zh-CN', open: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/deny-override-card.html?theme=${s.theme}&lang=${s.lang}`)
  await page.waitForSelector('[data-testid="deny-override-warning"]')
  const warning = await page.locator('[data-testid="deny-override-warning"]').innerText()
  const labels = (await page.locator('[data-capture-root] button').allInnerTexts()).map(t => t.trim())
  let ok = warning.includes('git-publish-push-protected-branch-name') && warning.includes('protected default branch') && !labels.includes('Trust')
  if (s.lang === 'en') ok = ok && !labels.includes('Allow once') && !labels.includes('Adjust approval mode')
  let items = []
  if (s.open) {
    await page.getByTestId('deny-override-trigger').click()
    await page.getByRole('menu').waitFor()
    await page.waitForFunction(() => {
      const el = document.querySelector('[role="menu"]')
      return !!el && getComputedStyle(el).opacity === '1'
    })
    items = (await page.getByRole('menuitem').allInnerTexts()).map(t => t.trim())
    ok = ok && items.length === 2
  }
  console.log(`${s.name}: buttons=${JSON.stringify(labels)} menu=${JSON.stringify(items)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('capture failed: at least one frame did not match its asserted state')
  process.exit(1)
}
