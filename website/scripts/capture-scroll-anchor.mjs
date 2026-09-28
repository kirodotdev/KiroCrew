/**
 * Screenshot harness for the reading-position restore (issue #2774).
 *
 * Runs the REAL SPA with every /api/** call and the /api/ws websocket
 * intercepted by Playwright and answered from fixtures — no gateway. The
 * virtualizer, ScrollAnchorCache, and localStorage run unmodified in the page,
 * so the capture IS the feature working end-to-end:
 *
 *   1. restore-position: open a long session, scroll up mid-history, switch to
 *      another session, switch back → the reading position is restored (and
 *      the jump-to-latest pill is visible) instead of the old pin-to-bottom.
 *   2. storage-debug: the Developer → Storage page showing the new
 *      vc_anchor_* category and the updated Clear Old Caches copy.
 *
 * Usage: node scripts/capture-scroll-anchor.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/scroll-position-restore-2774'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const mkMessages = (n, tag) => {
  const out = []
  for (let i = 0; i < n; i++) {
    out.push({
      role: i % 2 === 0 ? 'user' : 'assistant',
      ts: now - (n - i) * 60,
      content:
        i % 2 === 0
          ? `Question #${i} (${tag}): how does step ${i} of the pipeline behave?`
          : `Answer #${i} (${tag}): step ${i} reads its input, validates the schema, and hands the result to step ${i + 1}. `.repeat(3),
    })
  }
  return out
}

const slots = [
  {
    key: 'long-session', title: 'Long investigation', running: false,
    last_message: 'Answer #39', messages: 40, agent: 'kirocrew',
    memory_mode: 'persistent', modified: Math.floor(now),
  },
  {
    key: 'short-session', title: 'Quick question', running: false,
    last_message: 'Answer #3', messages: 4, agent: 'kirocrew',
    memory_mode: 'persistent', modified: Math.floor(now) - 300,
  },
]
const details = {
  'long-session': { running: false, has_more: false, total: 40, queue: [], messages: mkMessages(40, 'long') },
  'short-session': { running: false, has_more: false, total: 4, queue: [], messages: mkMessages(4, 'short') },
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  // Predicate, not the '**/api/**' glob — see capture-diff-render.mjs.
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (process.env.CAPTURE_DEBUG) console.log('API:', path)
    if (path === '/api/chat/slots') return json(route, slots)
    const m = path.match(/^\/api\/chat\/slots\/([^/]+)/)
    if (m) return json(route, details[decodeURIComponent(m[1])] || details['long-session'])
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/status') return json(route, { sessions: 2, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'long-session')
  })
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)

  const scroller = page.locator('[data-chat-scroller], .chat-scroller, main [class*="overflow-y-auto"]').first()

  // The transcript opens pinned to the bottom (unchanged default).
  const geom = async () => page.evaluate(() => {
    const els = [...document.querySelectorAll('main *')].filter(e =>
      e.scrollHeight > e.clientHeight + 500 && /auto|scroll/.test(getComputedStyle(e).overflowY))
    const el = els[0]
    return el ? { top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight } : null
  })
  console.log('entry geometry:', await geom())

  // 1. Scroll UP mid-history and let the anchor save settle.
  await page.evaluate(() => {
    const els = [...document.querySelectorAll('main *')].filter(e =>
      e.scrollHeight > e.clientHeight + 500 && /auto|scroll/.test(getComputedStyle(e).overflowY))
    const el = els[0]
    if (el) el.scrollTop = Math.max(0, el.scrollHeight * 0.35)
  })
  await page.waitForTimeout(600)
  const savedAnchor = await page.evaluate(() => localStorage.getItem('vc_anchor_long-session'))
  console.log('saved anchor:', savedAnchor)
  await page.screenshot({ path: `${OUT}/1-scrolled-up-reading.png` })

  // 2. Switch to the other session…
  await page.getByText('Quick question', { exact: false }).first().click()
  await page.waitForTimeout(1500)
  await page.screenshot({ path: `${OUT}/2-switched-away.png` })

  // 3. …and back. The reading position is restored (not the bottom), and the
  // jump-to-latest pill is visible.
  await page.getByText('Long investigation', { exact: false }).first().click()
  await page.waitForTimeout(1500)
  console.log('restored geometry:', await geom())
  await page.screenshot({ path: `${OUT}/3-position-restored.png` })

  // 4. Developer → Storage page with both cache families present.
  await page.evaluate(() => {
    localStorage.setItem('vc_heights_demo-1', JSON.stringify({ a: 100, b: 220 }))
    localStorage.setItem('vc_anchor_demo-1', JSON.stringify({ key: 'row-171234', top: -42 }))
  })
  await page.goto(BASE + '/developer', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.getByText('Storage', { exact: false }).first().click()
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/4-storage-debug-categories.png` })

  await browser.close()
  console.log('done →', OUT)
  void scroller
}

main().catch(err => { console.error(err); process.exit(1) })
