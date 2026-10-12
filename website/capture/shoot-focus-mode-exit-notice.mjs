/**
 * Screenshot harness for the focus-mode exit notice (issue #19270).
 * House pattern of website/capture/shoot-tree-download-menu.mjs: the REAL
 * built SPA (website/dist) behind the in-process loopback static server, every
 * /api/** answered from fixtures via Playwright route interception. Nothing
 * listens beyond 127.0.0.1 and no gateway is involved.
 *
 * Frames, per theme:
 *   focus-exit-notice-<theme>   focus mode turned on FROM THE KEYBOARD (Tab to
 *                               the toggle, Enter): the top bar and nav rail
 *                               are off screen and the shared shell notice
 *                               shows "Focus mode on. Press Alt + Shift + M to
 *                               turn it off." in display spelling.
 *   focus-pointer-<theme>       the same toggle clicked with the POINTER: the
 *                               chrome hides and no notice appears -- a pointer
 *                               user can hover an edge to peek it back.
 *
 * Usage (from website/, after `npm run build`):
 *   node capture/shoot-focus-mode-exit-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/focus-mode-exit-notice'
mkdirSync(OUT, { recursive: true })

// One live chat slot, so the chat page has a session to show. The stub's
// `/api/chat/slots/unrestored` guess ([]) is what the Redux slot list would
// otherwise be built from, and a slot without a `key` crashes the command
// palette's recents provider at boot; a real row keeps the shell up.
const SLOT = 'chat-1-1'
const slots = [{
  key: SLOT, title: 'Focus mode', running: false, last_message: 'Focus mode',
  messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: 'default',
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const slotDetail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = async (path, route) => {
  if (path === '/api/chat/slots') return json(route, slots), true
  if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  logPageProblems(page)
  await stubDashboardApi(page, { theme, slots, extra })
  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
  const toggle = page.getByTestId('focus-mode-toggle')
  await toggle.waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)

  // Keyboard activation: focus the button, press Enter.
  await toggle.focus()
  await page.keyboard.press('Enter')
  await page.getByText('Focus mode on. Press Alt + Shift + M to turn it off.').waitFor({ timeout: 5000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/focus-exit-notice-${theme}.png` })

  // Back out with the chord, then the pointer path: no notice.
  await page.keyboard.press('Alt+Shift+M')
  await page.waitForTimeout(400)
  await toggle.click()
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/focus-pointer-${theme}.png` })
  await page.close()
}

await browser.close()
srv.close()
console.log('wrote', OUT)
