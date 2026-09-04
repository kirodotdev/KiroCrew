/**
 * Screenshot harness for the COLLAPSED prose ```diff fence.
 *
 * Runs the real SPA with every /api/** call and the /api/ws websocket answered
 * from fixtures — no gateway, no dashboard token, no provider calls — and
 * photographs a message carrying THREE diff fences: first as the chip row a
 * reader now sees, then with one chip opened.
 *
 * The endpoint table below is deliberately a DATA MAP rather than the if-chain
 * `capture-diff-render.mjs` uses. That is not a style preference: the two files
 * answer the same boot endpoints, and an if-chain here was a 462-token clone of
 * that file under `.jscpd.json`'s 180-token floor. A table carries the same
 * fixtures without duplicating its shape, so neither file needs an exemption.
 *
 * Usage: node scripts/capture-prose-diff-fold.mjs <baseUrl> <outDir>
 *   PW_CHROMIUM=<path>  use an existing chromium build instead of the pinned one
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/prose-diff-fold'
const SLOT = 'chat-diff-fold-demo'

mkdirSync(OUT, { recursive: true })

const fence = (path, body) => ['```diff', `--- a/${path}`, `+++ b/${path}`, ...body, '```'].join('\n')

const BODY = [
  'Three files changed. The segment flush now returns a typed sentinel, the',
  'broadcast no longer blocks the event loop, and the panel follows the shared',
  'split preference.',
  '',
  fence('src/kiro_crew/dashboard/chat_runner.py', [
    '@@ -140,7 +140,9 @@',
    ' def flush_segment(self, seg):',
    '     if not seg.lines:',
    '-        return None',
    '+        return Segment.empty()',
    '+    seg.normalize()',
    '     buf = self._buffer',
  ]),
  '',
  'Broadcast path:',
  '',
  fence('src/kiro_crew/dashboard/state.py', [
    '@@ -88,3 +88,4 @@',
    '     def broadcast(self, event):',
    '-        self._queue.put(event)',
    '+        self._queue.put_nowait(event)',
    '+        self._metrics.count("broadcast")',
  ]),
  '',
  'And the panel:',
  '',
  fence('website/src/components/DiffPanel.tsx', [
    '@@ -37,2 +37,2 @@',
    "-      diffStyle: 'unified',",
    "+      diffStyle: sideBySide ? 'split' : 'unified',",
  ]),
  '',
  'All 174 tests pass.',
].join('\n')

const now = Math.floor(Date.now() / 1000)

/** Exact-path fixtures: the boot endpoints the chat page reads on mount. */
const EXACT = {
  '/api/chat/slots': [{
    key: SLOT,
    title: 'Prose diff fold demo',
    running: false,
    last_message: 'Three files changed.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: now,
  }],
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/chat/nav/resolve-links': { summaries: [] },
}

/** Prefix fixtures, longest-first so a specific rule beats a general one. */
const PREFIX = [
  ['/api/chat/slots/', {
    running: false,
    has_more: false,
    total: 2,
    queue: [],
    messages: [
      { role: 'user', content: 'Fix the flush sentinel, unblock the broadcast, and honour the split preference', ts: now - 600 },
      { role: 'assistant', ts: now - 60, content: BODY },
    ],
  }],
  ['/api/instances', { instances: [], active: '' }],
]

/** Endpoints answered with a status rather than a body. */
const STATUS = [['/api/files', 404]]

/** An unlisted endpoint: an object for the config-ish family, else a list. */
const OBJECTISH = /(config|tips|voice|autonudge|branding|status|usage-summary)/

function fixtureFor(path) {
  if (path in EXACT) return { body: EXACT[path] }
  for (const [prefix, body] of PREFIX) if (path.startsWith(prefix)) return { body }
  for (const [prefix, status] of STATUS) if (path.startsWith(prefix)) return { status }
  return { body: OBJECTISH.test(path) ? {} : [] }
}

async function main() {
  // PW_CHROMIUM lets a host whose ms-playwright cache holds a different build
  // than this package pins supply the binary, instead of re-downloading one.
  const browser = await chromium.launch(
    process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  )
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  // A predicate, not the '**/api/**' glob: on the Vite dev server that glob also
  // matches source URLs like /src/api/client.ts and would feed them JSON, which
  // kills the module graph and leaves a blank page.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const { body, status } = fixtureFor(new URL(route.request().url()).pathname)
    return status
      ? route.fulfill({ status, body: '' })
      : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  await page.addInitScript(slot => {
    for (const [k, v] of [
      ['mc-theme', 'dark'], ['mc-onboarded', '1'], ['mc-privacy-acked', '1'],
      // ChatPage namespaces the active-slot key by mode (`mc-active-slot-<mode>`).
      ['mc-active-slot', slot], ['mc-active-slot-chat', slot],
    ]) localStorage.setItem(k, v)
  }, SLOT)
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(5000)

  const chips = page.locator('[data-testid="prose-diff-chip"]')
  try {
    await chips.first().waitFor({ state: 'visible', timeout: 15000 })
  } catch (err) {
    await page.screenshot({ path: `${OUT}/DEBUG.png` })
    console.log('DEBUG frame written; url:', page.url(), '| root:',
      (await page.locator('#root').innerText().catch(() => '')).slice(0, 200).replace(/\n/g, ' / '))
    throw err
  }
  console.log('chips:', await chips.count())

  await page.screenshot({ path: `${OUT}/01-collapsed.png` })
  console.log('wrote', `${OUT}/01-collapsed.png`)

  await chips.first().click()
  await page.waitForTimeout(600)
  await page.mouse.move(0, 0)
  await page.waitForTimeout(200)
  await page.screenshot({ path: `${OUT}/02-one-open.png` })
  console.log('wrote', `${OUT}/02-one-open.png`)

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
