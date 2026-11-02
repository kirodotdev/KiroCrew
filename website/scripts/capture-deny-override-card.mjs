/**
 * Screenshots of the security-rule override card.
 *
 * Uses the isolated capture entry website/capture/deny-override-card.html, which
 * mounts the REAL ChatInput with the permission row the gateway emits when a
 * deny rule refused a command and a person may override it.
 *
 * Each frame ASSERTS its state before it is written: the warning leads with the
 * rule family's plain line, shows the refused command and keeps the rule id and
 * its expert text behind a closed "Technical details", the tool pill above reads as
 * waiting on the person rather than "Running", the bar has no "Allow once"
 * button, no "Trust" control and no approval-mode hint, the trigger is a menu
 * button, the open menu lists exactly the two override scopes and its "Allow
 * for this session" row names the rule family it lets through, and the Details frame
 * shows the rule's expert text and id once expanded.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort   # in another shell
 *   node scripts/capture-deny-override-card.mjs http://127.0.0.1:6825 ../temp-screenshots/deny-override
 *
 * Or, with no server at all, against a build of the capture entry: pass the
 * build's directory third, and every request under BASE is answered from it.
 *   node scripts/capture-deny-override-card.mjs http://capture.invalid OUT_DIR DIST_DIR [SUFFIX]
 * An optional fourth argument is appended to every file name.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/deny-override'
const DIST = process.argv[4] || ''
// Appended to every file name, so a re-capture can be attached under new names.
const SUFFIX = process.argv[5] || ''
mkdirSync(OUT, { recursive: true })

const TYPES = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.wasm': 'application/wasm',
}

const SCENES = [
  { name: 'deny-override-dark', theme: 'dark', lang: 'en', open: false },
  { name: 'deny-override-dark-menu', theme: 'dark', lang: 'en', open: true },
  { name: 'deny-override-light-menu', theme: 'light', lang: 'en', open: true },
  { name: 'deny-override-dark-zh', theme: 'dark', lang: 'zh-CN', open: true },
  { name: 'deny-override-dark-details', theme: 'dark', lang: 'en', open: false, details: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })
if (DIST) {
  // Answer from the build on disk; a path that leaves DIST or names nothing is a 404.
  await page.route(`${BASE}/**`, (route) => {
    const rel = normalize(decodeURIComponent(new URL(route.request().url()).pathname)).replace(/^[/\\]+/, '')
    const file = join(DIST, rel)
    if (rel.startsWith('..') || !existsSync(file)) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, contentType: TYPES[extname(file)] || 'application/octet-stream', body: readFileSync(file) })
  })
}

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/deny-override-card.html?theme=${s.theme}&lang=${s.lang}`)
  await page.waitForSelector('[data-testid="deny-override-warning"]')
  const warning = await page.locator('[data-testid="deny-override-warning"]').innerText()
  const details = page.getByTestId('deny-override-details')
  const detailsText = (await details.textContent()) || ''
  const detailsOpen = await details.evaluate(el => el.hasAttribute('open'))
  const pill = await page.getByTestId('capture-tool-pill').innerText()
  const labels = (await page.locator('[data-capture-root] button').allInnerTexts()).map(t => t.trim())
  const command = await page.getByTestId('deny-override-command').innerText()
  const haspopup = await page.getByTestId('deny-override-trigger').getAttribute('aria-haspopup')
  let ok = !detailsOpen && detailsText.includes('git-publish-push-protected-branch-name') && detailsText.includes('protected default branch')
    && !warning.includes('git-publish-push-protected-branch-name') && !labels.includes('Trust')
    && command.includes('git push origin main') && haspopup === 'menu' && !pill.includes('Running')
  if (s.lang === 'en') {
    ok = ok && warning.includes('stops pushes that could land straight on your main branch')
      && pill.includes('Waiting for approval') && !labels.includes('Allow once') && !labels.includes('Adjust approval mode')
  }
  if (s.lang === 'zh-CN') ok = ok && warning.includes('主分支') && pill.includes('等待审批')
  if (s.details) {
    await details.locator('summary').click()
    const open = await details.evaluate(el => el.hasAttribute('open'))
    const shown = await details.innerText()
    ok = ok && open && shown.includes('Rule: git-publish-push-protected-branch-name') && shown.includes('protected default branch')
  }
  let items = []
  if (s.open) {
    await page.getByTestId('deny-override-trigger').click()
    await page.getByRole('menu').waitFor()
    await page.waitForFunction(() => {
      const el = document.querySelector('[role="menu"]')
      return !!el && getComputedStyle(el).opacity === '1'
    })
    items = (await page.getByRole('menuitem').allInnerTexts()).map(t => t.trim())
    const familyRow = s.lang === 'en' ? 'stop this rule from blocking pushes that could land straight on your main branch' : '这条规则不再阻止可能直接推送到你主分支的操作'
    const endRow = s.lang === 'en' ? 'until you close this chat (Session options → Close session)' : '直到你关闭这个聊天（会话选项 → 关闭会话）'
    ok = ok && items.length === 2 && items[1].includes(familyRow) && items[1].includes(endRow)
  }
  console.log(`${s.name}: buttons=${JSON.stringify(labels)} menu=${JSON.stringify(items)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/${s.name}${SUFFIX}.png` })
}

await browser.close()
if (failed) {
  console.error('capture failed: at least one frame did not match its asserted state')
  process.exit(1)
}
