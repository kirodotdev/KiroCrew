/**
 * Screenshot harness for the C1 diff-render cleanup.
 *
 * Runs the REAL SPA (Vite dev server or built dist) with every /api/** call and
 * the /api/ws websocket intercepted by Playwright and answered from fixtures —
 * no gateway, no dashboard token, no provider calls. The client code under test
 * is unmodified: DiffBlock renders a fenced ```diff exactly as in production.
 *
 * Captures: unified (dark), split view (dark), unified (light) — showing the
 * single colored line-number gutter, the edge bars, the zigzag "unchanged
 * lines" separator, and forced line wrap.
 *
 * Usage: node scripts/capture-diff-render.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/diff-render-clean'
const SLOT = 'chat-diff-demo'

mkdirSync(OUT, { recursive: true })

const LONG_LINE = '        raise SegmentOverflowError(f"flush_segment: buffer {len(buf)} exceeds high-water mark {HIGH_WATER_MARK} for stream {stream_id!r} — refusing to enqueue further chunks")'

const DIFF = [
  '```diff',
  '--- a/src/kiro_crew/dashboard/chat_runner.py',
  '+++ b/src/kiro_crew/dashboard/chat_runner.py',
  '@@ -140,7 +140,9 @@',
  ' def flush_segment(self, seg):',
  '     if not seg.lines:',
  '-        return None',
  '+        return Segment.empty()',
  '+    seg.normalize()',
  '     buf = self._buffer',
  '     if len(buf) > HIGH_WATER_MARK:',
  '-        raise SegmentOverflowError("buffer overflow")',
  '+' + LONG_LINE.slice(0),
  '     return seg',
  '@@ -271,4 +273,4 @@',
  '     def close(self):',
  '-        self._flush(force=True)',
  '+        self._flush(force=True, drain=True)',
  '         self._buffer.clear()',
  '```',
].join('\n')

const slots = [{
  key: SLOT,
  title: 'Diff render demo',
  running: false,
  last_message: 'Updated flush_segment.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Fix the empty-segment flush and raise a descriptive overflow error', ts: Date.now() / 1000 - 600 },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Done — empty segments now return a typed sentinel and the overflow error carries the diagnostics:\n\n' + DIFF },
  ],
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

  // Predicate, not the '**/api/**' glob: on the Vite DEV server that glob also
  // matches source-module URLs like /src/api/client.ts and feeds them JSON,
  // which kills the module graph and leaves a blank page. Only true backend
  // calls have a pathname that STARTS with /api/.
  let themeMode = 'dark'
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (process.env.CAPTURE_DEBUG) console.log('API:', path)
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // Prerequisite gate: must be fully ready or the app renders the setup
    // chapter instead of the chat (reads .operation.status — shape matters).
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: themeMode, theme: '' })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    // DiffBlock's "Open" HEAD probe — 404 so the hover chrome stays minimal.
    if (path.startsWith('/api/files')) return route.fulfill({ status: 404, body: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  async function load(theme) {
    themeMode = theme
    await page.addInitScript(t => {
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-diff-demo')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  async function shotDiff(name) {
    const block = page.locator('.diff-block').first()
    try {
      await block.waitFor({ state: 'visible', timeout: 10000 })
    } catch (e) {
      await page.screenshot({ path: `${OUT}/DEBUG-${name}.png`, fullPage: false })
      console.log('DEBUG frame written; body text head:', (await page.locator('body').innerText()).slice(0, 300).replace(/\n/g, ' | '))
      throw e
    }
    await block.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. Unified view, dark: colored gutter, edge bars, zigzag separator, wrap.
  await load('dark')
  await shotDiff('01-unified-dark')

  // 2. Split view, dark (hover reveals the toggle; click it).
  await page.locator('.diff-block').first().hover()
  await page.getByTitle('Split view').click()
  await page.waitForTimeout(400)
  await page.mouse.move(0, 0)
  await page.waitForTimeout(200)
  await shotDiff('02-split-dark')

  // 3. Unified view, light-theme parity.
  await load('light')
  await shotDiff('03-unified-light')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
