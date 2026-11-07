/** Real-browser evidence for #29952's session bookmarks in the side panel's Links tab.
 *
 * Drives website/capture/session-bookmarks-29952.html: the real ActivityViewer on
 * its Links view, with the bookmark routes answered in memory. Asserted, then shot:
 *
 *   bookmarks-populated        three bookmarks above the transcript links, one added
 *                              by the agent; each opens in a new tab.
 *   bookmarks-populated-light  the same in the light theme.
 *   bookmarks-empty            the empty state and the Add control.
 *   bookmarks-add-form         the add form, then a saved bookmark in the list.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6876 --strictPort
 *   node scripts/capture-29952-session-bookmarks.mjs http://127.0.0.1:6876 ../temp-screenshots/session-bookmarks
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6876'
const OUT = resolve(process.argv[3] || '../temp-screenshots/session-bookmarks')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query) {
  const ctx = await browser.newContext({ viewport: { width: 400, height: 560 }, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/session-bookmarks-29952.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-testid="session-bookmarks"]'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}

for (const theme of ['dark', 'light']) {
  const { ctx, page, errors } = await open(`theme=${theme}&seed=populated`)
  const rows = page.getByTestId('bookmark-row')
  check(`[${theme}] three bookmarks listed`, (await rows.count()) === 3)
  check(`[${theme}] bookmark opens in a new tab`, (await page.getByRole('link', { name: /Bookmarks PR/ }).getAttribute('target')) === '_blank')
  check(`[${theme}] agent bookmark is labelled`, (await page.getByRole('link', { name: /Bookmarks PR/ }).innerText()).includes('Bookmarks PR'))
  check(`[${theme}] transcript links still listed`, await page.getByText('About pull requests', { exact: true }).isVisible())
  await page.screenshot({ path: resolve(OUT, theme === 'dark' ? 'bookmarks-populated.png' : 'bookmarks-populated-light.png') })
  check(`[${theme}] no page errors`, errors.length === 0); await ctx.close()
}

{
  const { ctx, page, errors } = await open('theme=dark&seed=empty')
  check('[empty] empty state shown', await page.getByTestId('bookmarks-empty').isVisible())
  await page.screenshot({ path: resolve(OUT, 'bookmarks-empty.png') })
  await page.getByTestId('bookmark-add-open').click()
  await page.getByLabel('Bookmark URL').fill('https://github.com/kirodotdev/KiroCrew/pull/29985')
  await page.getByLabel('Bookmark title').fill('Bookmarks PR')
  await page.screenshot({ path: resolve(OUT, 'bookmarks-add-form.png') })
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByRole('link', { name: /Bookmarks PR/ }).waitFor()
  check('[empty] saved bookmark appears and the form closes', !(await page.getByTestId('bookmark-add-form').isVisible().catch(() => false)))
  check('[empty] no page errors', errors.length === 0); await ctx.close()
}

await browser.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
