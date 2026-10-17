/**
 * Screenshot harness for "the chat waiting indicator is readable, not a bare
 * glyph row" (issue #13779).
 *
 * The defect: while a turn runs, ChatFooter's plain running state rendered ONLY
 * the decorative mascot carousel (`<img>` ghost poses, alt="", aria-hidden). When
 * those images did not paint — as they did not in the nightly GUI user test — a
 * first-time user was left staring at a short column of broken-image glyphs with
 * no text, unsure whether the assistant was working or broken, and assistive tech
 * got nothing to announce. The fix adds a visible, localized "Thinking…" label
 * beside the carousel (role=status), reading the same as the sidebar row.
 *
 * The evidence has to show the SAME running footer in two locales, because a lone
 * English "Thinking…" frame does not prove the string comes from the catalog —
 * only the non-English frame does. Frame 1 is the running footer in English
 * (mascot carousel + "Thinking…"); frame 2 is the same footer in Chinese, where
 * the label reads 思考中… from the catalog.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no
 * dashboard token, no provider CLI. A `running: true` slot + detail is exactly
 * what ChatPage turns into the running footer (see capture-above-composer-order).
 *
 * Usage: node scripts/capture-chat-waiting-indicator.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chat-waiting-indicator'
const ACTIVE = 'chat-waiting'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// A single running slot. The last turn is a settled `user` message and the slot
// is `running`, so the footer shows the PLAIN running branch (thinking / between
// steps) — the carousel plus the new readable label — rather than the streaming
// caret, the stopping text, or the compacting text.
const SLOTS = [{
  key: ACTIVE,
  title: 'Where does Kiro Crew store my data?',
  running: true,
  messages: 1,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  last_ts: '2026-09-25T00:10:00Z',
  folder_id: '',
  last_message: 'Where does Kiro Crew store my data?',
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  messages: [
    { role: 'user', ts: now - 3, content: 'Where does Kiro Crew store my data?' },
  ],
}

const extra = async (path, route) => {
  if (path === '/api/chat/slots') { await json(route, SLOTS); return true }
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  return false
}

/**
 * Element-screenshot the running footer. `data-testid="chat-footer"` is the
 * region the change lives in, so no hand-built clip arithmetic is needed.
 */
async function shotFooter(page, name) {
  const footer = page.getByTestId('chat-footer').first()
  await footer.waitFor({ state: 'visible', timeout: 20000 })
  const out = join(OUT, name)
  await footer.screenshot({ path: out })
  console.log('wrote', out)
}

async function captureLocale(browser, base, lang, expectLabel, name) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The label is ~13px; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    slots: SLOTS,
    extra,
    localStorageEntries: {
      'mc-lang': lang,
      'mc-active-slot': ACTIVE,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  // The assertion the frame exists for: the localized label rendered beside the
  // carousel. Waiting on it also replaces a fixed sleep.
  const label = page.getByText(expectLabel, { exact: true }).first()
  await label.waitFor({ state: 'visible', timeout: 20000 })
  // Brief settle so the carousel's first cross-fade beat lands in the frame.
  await page.waitForTimeout(600)
  await shotFooter(page, name)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  await captureLocale(browser, base, 'en', 'Thinking…', '01-thinking-en.png')
  await captureLocale(browser, base, 'zh-CN', '思考中…', '02-thinking-zh-CN.png')
  await browser.close()
  srv.close()
}

await main()
