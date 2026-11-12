/**
 * Screenshot harness for the redesigned chat tool-approval card.
 *
 * Serves the isolated capture entry (capture/approval-card-redesign.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL composer
 * in each of the card's five states, with the Trust and Reject menus open, with
 * the all-tools row armed, and after a decision, in a light and a dark theme.
 * Every row is fabricated inside the entry and every /api call is answered
 * here, so no gateway / kiro-cli / token is involved.
 *
 * It also reports, for the open Trust menu, whether the menu's box overlaps
 * the card's command box: the exact-command row names no command, so the
 * command must stay in view while the menu is open.
 *
 * Usage (from website/): node capture/shoot-approval-card-redesign.mjs [outDir] [light,dark themes]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/approval-card-redesign')
const THEMES = (process.argv[3] || 'dracula-light,dracula-dark').split(',')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 860, height: 520 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
// A decision (a POST) succeeds with an empty body; every background read is
// left to the dev server, which has no such route, as in a session with no
// gateway. Matched on the path's first segment: a `**/api/**` glob also catches
// Vite's own `/src/api/*` module requests.
await page.route(url => url.pathname.startsWith('/api/'), route => (
  route.request().method() === 'POST'
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    : route.continue()
))

const open = async (scene, theme) => {
  await page.goto(`${base}/capture/approval-card-redesign.html?scene=${scene}&theme=${theme}`)
  await page.locator('[data-approval-actions]').waitFor({ timeout: 30000 })
  await page.waitForTimeout(700)
}
const shoot = async (name, theme) => {
  const path = `${OUT}/${name}-${theme.endsWith('light') ? 'light' : 'dark'}.png`
  await page.screenshot({ path })
  console.log('wrote', path)
}
const rect = async sel => page.locator(sel).first().evaluate(el => {
  const r = el.getBoundingClientRect()
  return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }
})
const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
const trust = () => page.locator('[data-approval-actions] button', { hasText: 'Trust' })
const reject = () => page.locator('[data-approval-actions] button', { hasText: 'Reject' })

for (const theme of THEMES) {
  // The five states.
  for (const scene of ['shell', 'reads', 'env', 'mcp', 'none']) {
    await open(scene, theme)
    await shoot(`state-${scene}`, theme)
  }

  // Trust menu, as it opens: the session lifetime, every row.
  await open('reads', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-trust', theme)
  console.log(`  [${theme}] reads: trust menu overlaps the command box:`, overlaps(await rect('[role="menu"]'), await rect('[data-testid="approval-command"]')))

  // A timed lifetime chosen, and the all-tools row armed by its first click.
  await page.getByRole('menuitemradio').first().click()
  await page.getByRole('menuitem').last().click()
  await page.waitForTimeout(200)
  await shoot('menu-trust-hour-armed', theme)
  await page.keyboard.press('Escape')

  // The long command: the menu must clear a three-line command box too.
  await open('shell', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-trust-long-command', theme)
  console.log(`  [${theme}] shell: trust menu overlaps the command box:`, overlaps(await rect('[role="menu"]'), await rect('[data-testid="approval-command"]')))
  await page.keyboard.press('Escape')

  // Reject menu (unchanged), with its reason box.
  await reject().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-reject', theme)
  await page.keyboard.press('Escape')

  // After a decision: a timed family grant, then a rejection.
  await trust().click()
  await page.getByRole('menuitemradio').first().click()
  await page.getByRole('menuitem').nth(1).click()
  await page.locator('[data-testid="approval-receipt"]').waitFor()
  await page.waitForTimeout(500)
  await shoot('receipt-trusted', theme)

  await open('shell', theme)
  await reject().click()
  await page.getByRole('menuitem').first().click()
  await page.locator('[data-testid="approval-receipt"]').waitFor()
  await page.waitForTimeout(500)
  await shoot('receipt-rejected', theme)
}

await browser.close()
await server.close()
