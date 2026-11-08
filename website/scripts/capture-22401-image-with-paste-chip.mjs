/**
 * Screenshot harness for "an image sent with a collapsed paste shows its
 * markdown as text" (issue #22401).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no token.
 * The chat is opened cold with one user row the way a send writes it: the
 * producer's leading `![image](dest)` line, then the typed text around a
 * 3-line paste collapsed to its `[ Paste #1 · 3 lines ]` token, with the
 * paste block on `meta.pastes`.
 *
 * Frames (per theme): one `<name>-<theme>.png` of the user bubble. Run it on
 * a main build for `before` and on this branch's build for `after`.
 *
 * Usage (in website/): node scripts/capture-22401-image-with-paste-chip.mjs <before|after> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { mockShot } from './lib/mock-shot.mjs'

const NAME = process.argv[2] === 'before' ? 'before' : 'after'
const OUT = resolve(process.argv[3] || '../temp-screenshots/22401-image-with-paste-chip')
mkdirSync(OUT, { recursive: true })
const ACTIVE = 'img-paste'
const IMG = '/home/user/uploads/a_pasted-image-1.png'
const BLOCK = { id: 'p1', seq: 1, lines: 3, content: 'step one\nstep two\nstep three' }
// Same token formatToken writes (website/src/utils/pasteTokens.ts) for an id
// whose invisible encoding is not needed to find it (seq + lines match).
const TOKEN = `[ Paste #${BLOCK.seq} · ${BLOCK.lines} lines ]`
const CONTENT = `![image](${IMG})\n\nWhat is wrong in this screen?\n${TOKEN}\nThose are the steps I ran.`

const now = Math.floor(Date.now() / 1000)
const SLOTS = [{
  key: ACTIVE, title: 'Screen question', running: false, messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', modified: now, last_ts: '2026-11-02T21:00:00Z', folder_id: '',
  last_message: 'The dialog is clipped.', source_links: [], source_links_total: 0,
}]
const DETAIL = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 40, content: CONTENT, meta: { pastes: [BLOCK], files: [IMG], sendId: 's-1' } },
    { role: 'assistant', ts: now - 30, content: 'The dialog is clipped at the bottom edge.' },
  ],
}

let failures = 0
const check = (label, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  const SHOT = await mockShot(browser, 800, 500)
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme,
      slots: SLOTS,
      extra: async (path, route) => {
        if (path.startsWith('/api/chat/slots/')) { await json(route, DETAIL); return true }
        if (path === '/api/file-raw') { await route.fulfill({ status: 200, contentType: 'image/png', body: SHOT }); return true }
        return false
      },
      localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': ACTIVE, 'mc-privacy-notice-v1': '1' },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.getByText('clipped at the bottom edge', { exact: false }).last().waitFor({ timeout: 30000 })
    await page.waitForTimeout(600)
    const bubble = page.locator('.msg-content').first()
    const raw = await bubble.getByText('![image]', { exact: false }).count()
    const imgs = await bubble.locator('img').count()
    const chip = await bubble.locator('[data-paste-seq]').count()
    check(`[${theme}/${NAME}] raw image markdown drawn: ${raw > 0}`, NAME === 'before' ? raw > 0 : raw === 0)
    check(`[${theme}/${NAME}] <img> drawn: ${imgs}`, NAME === 'before' ? imgs === 0 : imgs === 1)
    check(`[${theme}/${NAME}] paste chip drawn`, chip > 0)
    check(`[${theme}/${NAME}] no page errors`, errors.length === 0)
    await bubble.screenshot({ path: resolve(OUT, `${NAME}-${theme}.png`) })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
