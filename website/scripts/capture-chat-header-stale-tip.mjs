/** Real-browser evidence on the REAL ChatPage: the session header's side-panel
 * toggle does not bring back an old hover tip when it reappears.
 *
 * The scene: hover the toggle until its tip shows, click it (the panel opens
 * and the toggle goes away under the pointer), move the pointer off to the
 * transcript, then close the panel with the side-panel chord. The toggle comes
 * back. On main its old tip comes back with it, pinned where it was, though
 * nothing is hovering; with the fix the header is quiet.
 *
 * Mounts capture/chatpage-scroll-shell.html (the real ChatPage, preloaded
 * store, animations off) with its `short` scene; every /api call is answered
 * by route interception, gateway-free. Serves the dev build itself (vite
 * in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light), clipped to the header's right end:
 *   <stage>-toggle-back-<theme>.png
 * where <stage> is `after` (the default) or `before`. The before frames are
 * this script run against origin/main's ChatPage.tsx and ChatTitleOverlay.tsx;
 * its checks then fail, since the stale tip is up.
 *
 * Usage (in website/):
 *   node scripts/capture-chat-header-stale-tip.mjs ../temp-screenshots/chat-header-stale-tip [after|before]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/chat-header-stale-tip')
const STAGE = process.argv[3] === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5197, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => { console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

// Must match the entry's `short` fixture: ChatPage refetches the slot on mount
// and the answer replaces the preloaded transcript.
const SHORT = [
  { role: 'user', content: 'Status check #1: anything new in the queue?', cls: '', ts: '2026-08-27T01:10:00Z' },
  { role: 'assistant', content: 'Sweep 1 done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry', cls: '', ts: '2026-08-27T01:10:30Z' },
]

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'slot-a', title: 'scroll-shell fixture', running: false, has_more: false, total: SHORT.length, messages: SHORT }) })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpage-scroll-shell.html?theme=${theme}&scene=short`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Status check #1:').first().waitFor()

  const toggle = page.getByRole('button', { name: 'Open activity panel' })
  await toggle.hover()
  await page.waitForTimeout(300)
  check(`[${theme}] hover shows the tip`, await page.getByRole('tooltip').count() === 1)
  await toggle.click()
  await toggle.waitFor({ state: 'detached' })
  // Off the header, onto the transcript: nothing is hovering the toggle now.
  await page.mouse.move(300, 400)
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('toggle-activity-panel')))
  await toggle.waitFor()
  await page.waitForTimeout(300)
  check(`[${theme}] the toggle is back with no tip up`, await page.getByRole('tooltip').count() === 0)
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  await page.screenshot({ path: resolve(OUT, `${STAGE}-toggle-back-${theme}.png`), clip: { x: 1280 - 460, y: 0, width: 460, height: 110 } })
  await page.close()
}
await browser.close()
await server.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
