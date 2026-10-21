/** Real-browser evidence for #11823's two session-menu entries: "Reference in
 * chat" and "Copy session ID".
 *
 * Drives website/capture/session-menu-11823.html — the real SessionActionsMenu
 * inside an open dropdown, seeded with one session, rendered as a sidebar row
 * renders it (onReferenceInChat passed). Asserted, then shot, in dark and light:
 *
 *   session-menu        both new entries present: "Reference in chat" (above
 *                       Copy link) and "Copy session ID" (below it), each with
 *                       its own glyph; selecting "Reference in chat" fires its
 *                       handler once.
 *   session-menu-light  the same menu in the light theme.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6875 --strictPort
 *   node scripts/capture-11823-session-menu.mjs http://127.0.0.1:6875 ../temp-screenshots/session-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6875'
const OUT = resolve(process.argv[3] || '../temp-screenshots/session-menu')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query, size = { width: 420, height: 640 }) {
  const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/session-menu-11823.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[role="menu"]'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}
const hasIcon = (page, label, name) =>
  page.getByRole('menuitem').filter({ hasText: label }).first()
    .evaluate((el, n) => !!el.querySelector(`svg.lucide-${n}`), name)
    .catch(() => false)
const refCalls = page => page.evaluate(() => window.__refCalls)

// Dark theme — both entries present with their glyphs, in order around Copy link.
{
  const { ctx, page, errors } = await open('theme=dark')
  const items = await page.getByRole('menuitem').allInnerTexts()
  const labels = items.map(t => t.split('\n')[0].trim())
  check('[dark] Reference in chat present', labels.includes('Reference in chat'))
  check('[dark] Copy session ID present', labels.includes('Copy session ID'))
  check('[dark] Copy link still present', labels.includes('Copy link'))
  const iRef = labels.indexOf('Reference in chat')
  const iLink = labels.indexOf('Copy link')
  const iId = labels.indexOf('Copy session ID')
  check('[dark] Reference in chat sits just above Copy link', iRef >= 0 && iLink === iRef + 1)
  check('[dark] Copy session ID sits just below Copy link', iId === iLink + 1)
  check('[dark] Reference in chat uses the messages glyph', await hasIcon(page, 'Reference in chat', 'messages-square'))
  check('[dark] Copy session ID uses the hash glyph', await hasIcon(page, 'Copy session ID', 'hash'))
  await page.screenshot({ path: resolve(OUT, 'session-menu.png') })
  // Selecting Reference in chat fires its handler exactly once.
  await page.getByRole('menuitem', { name: 'Reference in chat' }).click(); await page.waitForTimeout(200)
  check('[dark] Reference in chat fires its handler once', (await refCalls(page)) === 1)
  check('[dark] no page errors', errors.length === 0); await ctx.close()
}
// Light theme still shows both.
{
  const { ctx, page, errors } = await open('theme=light')
  check('[light] the page is in the light theme', (await page.evaluate(() => document.documentElement.getAttribute('data-mode'))) === 'light')
  const labels = (await page.getByRole('menuitem').allInnerTexts()).map(t => t.split('\n')[0].trim())
  check('[light] both new entries present', labels.includes('Reference in chat') && labels.includes('Copy session ID'))
  await page.screenshot({ path: resolve(OUT, 'session-menu-light.png') })
  check('[light] no page errors', errors.length === 0); await ctx.close()
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
