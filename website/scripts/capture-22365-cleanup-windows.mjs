/** Real-browser evidence for the 14 and 30 day presets in the Clean up sessions panel.
 *
 * Drives website/capture/cleanup-sessions-windows.html: the real `ChatSidebar` over seeded rows,
 * with the dry-run preview answered from their ages. Each scene opens the header ⋮ menu, picks
 * "Clean up sessions", clicks a preset, then shoots the panel. Asserted:
 *
 *   dark-14     at the sidebar's default 260 px: all five presets render in order (1, 3, 7, 14,
 *               30 days), the row wraps onto two lines with no horizontal overflow, clicking
 *               14 days selects it and requests the preview with max_inactive_days=14 (2 seeded
 *               sessions qualify)
 *   light-30    the same in the light theme with 30 days (1 qualifies; the 60-day pinned row
 *               is kept)
 *   narrow-180  at SIDEBAR_MIN (180 px) the chips still wrap inside the panel with no overflow
 * Also asserted at 400 px (not shot): the five chips fit on one row.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6877 --strictPort
 *   node scripts/capture-22365-cleanup-windows.mjs http://127.0.0.1:6877 ../temp-screenshots/cleanup-windows
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6877'
const OUT = resolve(process.argv[3] || '../temp-screenshots/cleanup-windows')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

const PANEL = 'text=Clean Up Sessions >> xpath=ancestor::div[contains(@class,"rounded-lg")][1]'

async function open(query, days, size = { width: 900, height: 640 }) {
  const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/cleanup-sessions-windows.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-capture-ready]')
  await page.getByLabel('More options').first().click()
  await page.getByText('Clean up sessions', { exact: true }).click()
  await page.waitForSelector(PANEL)
  await page.waitForFunction(() => /will be moved|No inactive/.test(document.body.innerText))
  await page.getByRole('button', { name: `${days} days`, exact: true }).click()
  await page.waitForFunction(d => window.__cleanupPreviews.includes(d), days)
  await page.waitForFunction(() => /will be moved|No inactive/.test(document.body.innerText))
  // Park the pointer away from the chips so no preset carries a hover tint in the still.
  await page.mouse.move(size.width - 10, 10); await page.waitForTimeout(400)
  return { ctx, page, errors, panel: page.locator(PANEL) }
}
const previews = page => page.evaluate(() => window.__cleanupPreviews)
const chips = panel => panel.locator('button').evaluateAll(els => els.map(e => e.innerText.trim()).filter(t => /^\d+ days?$/.test(t)))
const selected = panel => panel.locator('button.bg-accent').first().innerText().then(t => t.trim())
const rowsUsed = panel => panel.locator('button').evaluateAll(els => {
  const tops = els.filter(e => /^\d+ days?$/.test(e.innerText.trim())).map(e => Math.round(e.getBoundingClientRect().top))
  return new Set(tops).size
})
const overflow = panel => panel.evaluate(el => el.scrollWidth - el.clientWidth)
const EXPECTED = ['1 day', '3 days', '7 days', '14 days', '30 days']

// 1 dark, 14 days, default sidebar width
{
  const { ctx, page, errors, panel } = await open('theme=dark', 14)
  check('[dark-14] five presets in order', JSON.stringify(await chips(panel)) === JSON.stringify(EXPECTED))
  check('[dark-14] 14 days is selected', (await selected(panel)) === '14 days')
  check('[dark-14] preview requested with 14', (await previews(page)).includes(14))
  check('[dark-14] chips wrap onto two rows at 260 px', (await rowsUsed(panel)) === 2)
  check('[dark-14] the panel has no horizontal overflow', (await overflow(panel)) <= 0)
  check('[dark-14] two sessions qualify', (await panel.innerText()).includes('2 sessions will be moved'))
  await panel.screenshot({ path: resolve(OUT, 'cleanup-windows-dark-14.png') })
  check('[dark-14] no page errors', errors.length === 0); await ctx.close()
}
// 2 light, 30 days
{
  const { ctx, page, errors, panel } = await open('theme=light', 30)
  check('[light-30] the page is in the light mode', (await page.evaluate(() => document.documentElement.getAttribute('data-mode'))) === 'light')
  check('[light-30] 30 days is selected', (await selected(panel)) === '30 days')
  check('[light-30] preview requested with 30', (await previews(page)).includes(30))
  check('[light-30] one session qualifies, pinned kept', (await panel.innerText()).includes('1 session will be moved'))
  await panel.screenshot({ path: resolve(OUT, 'cleanup-windows-light-30.png') })
  check('[light-30] no page errors', errors.length === 0); await ctx.close()
}
// 3 SIDEBAR_MIN: the row wraps rather than overflowing
{
  const { ctx, errors, panel } = await open('theme=dark&width=180', 14)
  check('[narrow-180] chips wrap onto more than one row', (await rowsUsed(panel)) >= 2)
  check('[narrow-180] the panel has no horizontal overflow', (await overflow(panel)) <= 0)
  await panel.screenshot({ path: resolve(OUT, 'cleanup-windows-narrow-180.png') })
  check('[narrow-180] no page errors', errors.length === 0); await ctx.close()
}
// 4 a wider sidebar keeps the five chips on one row (not shot). The viewport is wide enough
// that the sidebar's room clamp (window minus the chat pane minimum) does not hold it under 400.
{
  const { ctx, errors, panel } = await open('theme=dark&width=400', 14, { width: 1400, height: 640 })
  check('[wide-400] chips fit one row', (await rowsUsed(panel)) === 1)
  check('[wide-400] no page errors', errors.length === 0); await ctx.close()
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
