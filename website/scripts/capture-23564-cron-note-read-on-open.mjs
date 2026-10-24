/**
 * #23564 — opening a cron job's chat marks that run's bell note read.
 *
 * The REAL built SPA, gateway-free (stub-dashboard-api), opened on /chat with
 * the "Nightly digest" cron session active. The bell holds two unread cron
 * notes: one for that session, one for another cron session ("Weekly backup").
 *
 *   before  (main's dist):  the bell still counts 2; nothing is marked read.
 *   after   (this branch):  the bell counts 1; exactly one mark-read request,
 *                           for the open session's note.
 *
 * Asserted before each PNG: the bell's count and the mark-read requests made.
 *
 * Usage (in website/):
 *   node scripts/capture-23564-cron-note-read-on-open.mjs <distDir> <outDir> <before|after>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const DIST = resolve(process.argv[2] || 'dist')
const OUT = process.argv[3] || '../temp-screenshots/23564-cron-note-read'
const NAME = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slot = (key, title, msg) => ({
  key, title, running: false, messages: 2, agent: 'kirocrew', modified: now,
  last_ts: new Date().toISOString(), folder_id: '', last_message: msg,
  source_links: [], source_links_total: 0, tags: [],
})
const SLOTS = [
  slot('cron-digest', 'Nightly digest', 'Digest posted: 4 merged PRs, 1 failing pipeline.'),
  slot('cron-backup', 'Weekly backup', 'Backup verified.'),
]
const NOTES = [
  { kind: 'cron', title: 'Nightly digest', body: 'Digest posted: 4 merged PRs, 1 failing pipeline.', ts: '1761300000.000001', slot: 'cron-digest', job_id: 'job-digest', acked: false },
  { kind: 'cron', title: 'Weekly backup', body: 'Backup verified.', ts: '1761300000.000002', slot: 'cron-backup', job_id: 'job-backup', acked: false },
]
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'Post the nightly digest.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 60, content: 'Digest posted: 4 merged PRs, 1 failing pipeline.', cls: 'msg msg-assistant' },
  ],
}

const acks = []
const extra = (path, route) => {
  if (path === '/api/notifications') return json(route, { notifications: NOTES, unread: NOTES.length }), true
  if (path === '/api/notifications/ack') {
    acks.push(JSON.parse(route.request().postData() || '{}').ts)
    return json(route, { ok: true }), true
  }
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

const expected = NAME === 'before'
  ? { count: 2, acks: [] }
  : { count: 1, acks: [NOTES[0].ts] }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
let failures = 0
try {
  for (const theme of ['dark', 'light']) {
    acks.length = 0
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: SLOTS, theme, extra,
      localStorageEntries: { 'mc-active-slot': 'cron-digest', 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true' },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    const bell = page.getByRole('button', { name: 'Notifications', exact: true })
    await bell.waitFor({ state: 'visible', timeout: 20_000 })
    await page.getByText('Post the nightly digest.').first().waitFor({ timeout: 20_000 })
    // Let the notes load and any mark-read land.
    await page.waitForTimeout(2500)
    const title = await bell.getAttribute('title')
    const count = Number((title || '').match(/\d+/)?.[0] ?? 0)
    const ok = count === expected.count && JSON.stringify(acks) === JSON.stringify(expected.acks)
    console.log(`${NAME}/${theme}: bell="${title}" acks=${JSON.stringify(acks)} -> ${ok ? 'ok' : 'FAIL'}`)
    if (!ok) failures++
    await page.screenshot({ path: join(OUT, `${NAME}-${theme}.png`) })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
