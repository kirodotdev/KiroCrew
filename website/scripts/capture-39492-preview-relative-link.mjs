/** Real-browser evidence for #39492: a relative link in a side-panel markdown
 *  preview opens the file it names, and a missing one no longer opens a tab.
 *
 * Drives website/capture/preview-relative-link.html (the REAL MarkdownRenderer
 * under BasePathCtx with an open handler). Per theme it records one frame
 * after a plain click on the existing link and asserts:
 *  - the click reached the open handler with the joined path (after) or
 *    opened a new tab (before);
 *  - a plain click on the missing link opens no tab (after).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6839 --strictPort    # in another shell (website/)
 *   node scripts/capture-39492-preview-relative-link.mjs <before|after> http://127.0.0.1:6839 ../temp-screenshots/39492
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const NAME = process.argv[2] === 'before' ? 'before' : 'after'
const BASE = process.argv[3] || 'http://127.0.0.1:6839'
const OUT = process.argv[4] || '../temp-screenshots/39492'
mkdirSync(OUT, { recursive: true })
const SIBLING = '/home/user/project/spec/sibling.md'

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 760, height: 360 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.route('**/api/file-kinds', async route => {
    const { paths = [] } = JSON.parse(route.request().postData() || '{}')
    const kinds = Object.fromEntries(paths.map(p => [p, p === SIBLING ? 'file' : 'missing']))
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ kinds }) })
  })
  await page.goto(`${BASE}/capture/preview-relative-link.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  let tabs = 0
  context.on('page', p => { tabs++; p.close().catch(() => {}) })
  await page.getByRole('link', { name: 'the removed note' }).click()
  await page.getByRole('link', { name: 'the sibling note' }).click()
  await page.waitForTimeout(500)
  const opened = await page.getByTestId('opened').textContent()
  if (NAME === 'after') {
    check(`[${theme}/after] sibling opened in panel`, opened?.includes(SIBLING))
    check(`[${theme}/after] no new tab`, tabs === 0)
  } else {
    check(`[${theme}/before] clicks opened new tabs`, tabs > 0)
  }
  await page.screenshot({ path: `${OUT}/${NAME}-${theme}.png` })
  await context.close()
}
await browser.close()
process.exit(failures ? 1 : 0)
