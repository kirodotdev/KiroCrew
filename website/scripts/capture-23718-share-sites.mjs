/** Real-browser evidence for #23718: the share dialog under a policy `sites` list.
 *
 * Drives website/capture/share-sites.html (the REAL ShareMessageModal over a
 * fetch stub). Asserted per theme:
 *   all:     both site buttons, enabled.
 *   one:     LinkedIn only; no X button.
 *   failed:  no site button; the inline notice names the failed check.
 *   loading: both site buttons drawn disabled.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort    # in another shell (website/)
 *   node scripts/capture-23718-share-sites.mjs http://127.0.0.1:6825 ../temp-screenshots/23718
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/23718'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  for (const scene of ['all', 'one', 'failed', 'loading']) {
    const page = await browser.newPage({ viewport: { width: 1100, height: 760 } })
    page.on('pageerror', e => { console.error(`[${theme}/${scene}] pageerror:`, e.message); failures++ })
    await page.goto(`${BASE}/capture/share-sites.html?theme=${theme}&scene=${scene}`, { waitUntil: 'load' })
    await page.getByTestId('share-download').waitFor({ state: 'visible', timeout: 15000 })
    const x = page.getByTestId('share-x')
    const li = page.getByTestId('share-linkedin')
    if (scene === 'all') {
      await x.waitFor({ state: 'visible' })
      check(`[${theme}] all: both enabled`, (await x.isEnabled()) && (await li.isEnabled()))
    } else if (scene === 'one') {
      await page.waitForFunction(() => !document.querySelector('[data-testid="share-x"]'))
      check(`[${theme}] one: linkedin only`, (await li.isEnabled()) && (await x.count()) === 0)
    } else if (scene === 'failed') {
      await page.getByTestId('share-sites-error').waitFor({ state: 'visible' })
      check(`[${theme}] failed: no site, notice shown`, (await x.count()) === 0 && (await li.count()) === 0)
    } else {
      check(`[${theme}] loading: both disabled`, (await x.isDisabled()) && (await li.isDisabled()))
    }
    await page.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
    await page.close()
  }
}
await browser.close()
if (failures) { console.error(`${failures} failure(s)`); process.exit(1) }
