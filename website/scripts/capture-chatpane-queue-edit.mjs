/**
 * Screenshot harness for issue #2240: ChatPane (split view) now threads
 * onEdit into QueueStack, so the Pencil edit affordance — previously
 * unreachable in split panes — appears on a queued message card and opens
 * the inline editor.
 *
 * Runs the REAL SPA with every /api/** call and the /api/ws websocket
 * intercepted by Playwright and answered from fixtures — no gateway.
 * A persisted 2-pane split layout (mc-split-layouts) anchored at the active
 * slot auto-enters split mode on load. The left pane's slot detail carries a
 * queued message, so its QueueStack renders one interactive card.
 *
 * Captures:
 *   1. split-queue-pencil: split view with the queued card showing the
 *      Pencil button (asserted present via its aria-label before shooting).
 *   2. split-queue-editor-open: after clicking the Pencil, the inline
 *      EditInput replaces the card content.
 *
 * Usage: node scripts/capture-chatpane-queue-edit.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/chatpane-queue-edit'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000

const slots = [
  {
    key: 'pane-a', title: 'Design notes', running: false,
    last_message: 'Working through phase one…', messages: 3,
    agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now),
  },
  {
    key: 'pane-b', title: 'Release checklist', running: true,
    last_message: 'Summarized the layout options.', messages: 2,
    agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - 60,
  },
]

// hydrateSlotMessages deliberately no-ops for the ACTIVE slot (its transcript
// is owned by ChatPage's own hydration path), so the queued card must live in
// the BACKGROUND pane (pane-b): its ChatPane hydrates from chatSlotDetail and
// its QueueStack renders the interactive card this capture is about.
const detailA = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: 'Compare the two layout options.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: 'Option A keeps the sidebar fixed; option B collapses it under 900px.', cls: 'msg msg-assistant' },
  ],
}
const detailB = {
  running: true, has_more: false, total: 3, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'Run the deployment checklist for the new release.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 60, content: 'Starting phase one: build verification. This will take a few minutes.', cls: 'msg msg-assistant' },
    { role: 'queued', ts: now - 10, content: 'Also check the staging environment logs afterwards.', cls: 'msg msg-queued', meta: { queueId: 'q-demo-1' } },
  ],
}

// Persisted split layout anchored at pane-a: ChatPage auto-enters split mode
// when the active slot is the anchor of a live >= 2-member layout.
const splitLayouts = {
  'pane-a': {
    type: 'split', id: 'seed-split', dir: 'col',
    children: [
      { type: 'leaf', id: 'seed-a', kind: 'session', slot: 'pane-a' },
      { type: 'leaf', id: 'seed-b', kind: 'session', slot: 'pane-b' },
    ],
    sizes: [0.5, 0.5],
  },
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  // session_grid gates split view (splitFeatureEnabled in ChatPage) — without
  // it the persisted layout is ignored and the app stays in single chat.
  '/api/dashboard/config': { session_grid: true },
}

async function preparePage(context) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path in FIXTURES) return json(route, FIXTURES[path])
    // Queue mutations (PATCH edit, DELETE cancel, reorder) get a plain ack —
    // answering them with a slot-detail body would feed the client a bogus
    // shape at exactly the moment this capture exercises the edit path.
    if (route.request().method() !== 'GET') return json(route, { ok: true })
    const slotMatch = path.match(/^\/api\/chat\/slots\/([^/]+)/)
    if (slotMatch) return json(route, decodeURIComponent(slotMatch[1]) === 'pane-b' ? detailB : detailA)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))
  await page.addInitScript((layouts) => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'pane-a')
    localStorage.setItem('mc-split-layouts', layouts)
  }, JSON.stringify(splitLayouts))
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  // Readiness is gated by the Pencil waitFor in main(), not a fixed sleep —
  // fixed sleeps are the usual source of harness flake on a loaded runner.
  return page
}

async function main() {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2,
  })
  const page = await preparePage(ctx)

  // ---- 1. Split view: queued card shows the Pencil (the #2240 fix) ----
  console.log('panes:', await page.locator('[data-chat-pane]').count(),
    '| queued text:', await page.getByText('Also check the staging environment logs afterwards.').count(),
    '| pencil nodes:', await page.getByLabel('Edit queued message').count())
  const pencil = page.getByLabel('Edit queued message')
  await pencil.waitFor({ state: 'visible', timeout: 15000 })
  const queuedVisible = await page.getByText('Also check the staging environment logs afterwards.').first().isVisible()
  console.log('queued card visible:', queuedVisible, '| pencil count:', await pencil.count())
  if (!queuedVisible) throw new Error('queued card not visible in split pane')
  await page.screenshot({ path: `${OUT}/1-split-queue-pencil.png` })

  // ---- 2. Clicking the Pencil opens the inline editor ----
  await pencil.first().click()
  const editor = page.getByLabel('Edit queued message').and(page.locator('input'))
  await editor.waitFor({ state: 'visible', timeout: 5000 })
  const value = await editor.inputValue()
  console.log('editor open, initial value:', JSON.stringify(value))
  if (!value.includes('staging environment logs')) throw new Error('editor did not open with the queued content')
  await page.screenshot({ path: `${OUT}/2-split-queue-editor-open.png` })

  await ctx.close()
  await browser.close()
  console.log('done →', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
