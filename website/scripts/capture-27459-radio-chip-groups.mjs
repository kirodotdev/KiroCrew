/**
 * #27459 evidence: one-of-N chip groups drawn as radio groups.
 *
 * Shoots the same fixture surfaces on any checkout (the harness imports only
 * components that exist on main too), so running it on main and on the branch
 * gives a before/after pair. Also records each surface's accessible shape so
 * the pair shows what changed for a screen reader, which pixels cannot.
 *
 *   node scripts/capture-27459-radio-chip-groups.mjs <base-url> <out-dir>
 */
import { chromium } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:6813'
const out = process.argv[3] || '../temp-screenshots/27459'
mkdirSync(out, { recursive: true })
const browser = await chromium.launch()
const report = {}
try {
  const page = await browser.newPage({ viewport: { width: 760, height: 520 } })
  await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Capture has no gateway' }),
  }))
  for (const theme of ['dark', 'light']) {
    await page.goto(`${base}/capture/radio-chip-groups.html?theme=${theme}`)
    await page.getByTestId('harness').waitFor()
    await page.getByText('System', { exact: true }).waitFor()
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-groups.png` })
    // Keyboard: Tab into the Font group, then one arrow key.
    await page.locator('body').click({ position: { x: 5, y: 5 } })
    await page.keyboard.press('Tab')
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-tab-focus.png` })
    await page.keyboard.press('ArrowRight')
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-after-arrow.png` })
    report[`${theme}-after-arrow`] = await page.evaluate(() =>
      [...document.querySelectorAll('[data-testid="settings-group"] button')].map(b =>
        `${b.textContent.trim()} checked=${b.getAttribute('aria-checked') ?? b.getAttribute('aria-pressed')} focused=${b === document.activeElement}`))
    await page.keyboard.press('Space')
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-after-space.png` })
    report[theme] = await page.evaluate(() => {
      const describe = el => ({
        role: el.getAttribute('role') || el.tagName.toLowerCase(),
        name: el.getAttribute('aria-label') || el.textContent.trim(),
        pressed: el.getAttribute('aria-pressed'),
        checked: el.getAttribute('aria-checked'),
        tabIndex: el.tabIndex,
        focused: el === document.activeElement,
      })
      const sel = '[data-testid="settings-group"] [role], [data-testid="settings-group"] button, [data-testid="cc-settings"] .cc-every [role], [data-testid="cc-settings"] .cc-every button'
      return [...new Set(document.querySelectorAll(sel))].map(describe)
    })
  }
  writeFileSync(`${out}/a11y-shape.json`, JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report.dark, null, 1))
} finally {
  await browser.close()
}
