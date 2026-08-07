/**
 * Playwright end-to-end drive of the WeChat (Weixin/iLink) settings panel.
 *
 * Runs the REAL built SPA (website/dist) on a tiny loopback static server with
 * SPA fallback, with every /api/** call answered from fixtures — no gateway, no
 * dashboard credential. Same technique as capture-overview.mjs.
 *
 * Asserts the full QR login flow the user actually performs:
 *   1. Channels tab lists WeChat and shows "Needs setup"
 *   2. Clicking "Connect via QR" starts a session and renders the QR image
 *   3. Polling reports "scaned" -> the UI says confirm on your phone
 *   4. Polling reports "confirmed" -> success state, config refetched as connected
 *   5. Switching DM policy to allowlist reveals the allow-list editor
 *
 * Usage: node scripts/test-weixin-panel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/weixin-shots'
const PORT = 6813
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json',
}
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (!existsSync(file) || path === '/') file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch { res.writeHead(404); res.end() }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const json = (route, body) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

// A 1x1 red PNG stands in for Tencent's QR image (the panel only renders it).
// The dashboard endpoint returns a rendered PNG **data URI** (the backend
// encodes iLink's scannable login URL into a QR image server-side — see
// weixin_qr._render_qr_data_uri). Keep this fixture a data URI: modeling it as
// the raw iLink URL would re-mask the exact production bug this shape fixed.
const FAKE_QR =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

// Server-side login state the fixtures mutate as the flow progresses.
let qrPhase = 'waiting'   // waiting -> scaned -> confirmed
let wxConnected = false
let wxCredential = false
let dmPolicy = 'open'
let allowed = []
const calls = { start: 0, status: 0, save: [] }

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 950 }, deviceScaleFactor: 2 })
const page = await context.newPage()
await page.routeWebSocket(/\/api\/ws/, () => {})

const fails = []
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))

await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const p = url.pathname
  const method = route.request().method()

  if (p === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (p === '/api/kiro-prerequisite') return json(route, {
    platform: 'gateway', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: true,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { status: 'idle', message: '' },
  })
  if (p === '/api/themes') return json(route, { themes: [], installed: [] })
  if (p === '/api/status') return json(route, { sessions: 1, messages: 2, version: '0.1.0' })
  if (p === '/api/config') return json(route, { dashboard: {}, agent: {} })

  // ── the channel under test ──
  if (p === '/api/weixin/config' && method === 'GET') {
    return json(route, {
      connected: wxConnected, connect_error: '',
      configured: wxConnected, read_only: false,
      credential_set: wxCredential, enabled: false,
      account_id: wxConnected ? 'a5ace6fd482e@im.bot' : '',
      dm_policy: dmPolicy, allowed_user_ids: allowed,
    })
  }
  if (p === '/api/weixin/config' && method === 'PUT') {
    const body = JSON.parse(route.request().postData() || '{}')
    calls.save.push(body)
    if ('dm_policy' in body) dmPolicy = body.dm_policy
    if ('allowed_user_ids' in body) allowed = body.allowed_user_ids
    return json(route, { ok: true, restart_required: true })
  }
  if (p === '/api/channels/weixin/qr/start') {
    calls.start++
    qrPhase = 'waiting'
    return json(route, { session_id: 'sess-abc', qrcode_img_content: FAKE_QR })
  }
  if (p === '/api/channels/weixin/qr/status') {
    calls.status++
    if (qrPhase === 'confirmed') {
      wxConnected = true; wxCredential = true
      return json(route, { status: 'confirmed', connected: true, account_id: 'a5ace6fd482e@im.bot' })
    }
    return json(route, { status: qrPhase })
  }

  // Other channels the Channels tab polls for status. Shapes must be complete
  // enough that a sibling panel rendering first does not crash the shell.
  if (/\/api\/(slack|discord|telegram|webex|wecom)\/config$/.test(p)) {
    return json(route, {
      connected: false, connect_error: '', configured: false, read_only: false,
      enabled: false, bot_token_set: false, bot_token_preview: '',
      allowed_users: [], allowed_user_ids: [], allowed_emails: [],
      allowed_enterprise_ids: [], allowed_thread_ids: [], allowed_forum_chat_ids: [],
      tracking_channels: [], open_channels: [], trusted_bot_ids: [],
      reactions: {}, reactions_enabled: true, allow_forum: false,
      soft_threshold_pct: 80, hard_threshold_pct: 95,
      command: 'kirocrew', allow_all_users: false, ws_url: '',
    })
  }

  // ── app-shell routes (shapes matter: a wrong one crashes the boundary) ──
  // Governance gate (added by the channels-policy work): every member must be
  // explicitly permitted or the Settings panel is replaced by
  // ChannelDisabledPanel ("Off by admin") and never renders.
  if (p === '/api/governance/channels') return json(route, {
    slack: true, discord: true, telegram: true, webex: true, wecom: true, weixin: true,
  })
  if (p === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (p === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (p === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (p.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  if (p === '/api/models') return json(route, { models: [], default: 'auto' })
  if (p === '/api/chat/slots') return json(route, [])
  if (p === '/api/kirocrew-config' || p === '/api/config/kirocrew') return json(route, {
    agents: { kirocrew: { provider: 'kiroacp', model: 'auto', approval_mode: 'reads' } },
    default_agent: 'kirocrew',
    workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
    default_workspace: 'default',
    agent: { default_agent: 'kirocrew', provider: 'kiroacp', model: 'auto' },
    session: { timeout_secs: 900 },
    memory: { embedding_provider: 'local' },
    auto_update: true,
  })

  // Default: object for config-ish paths, ARRAY otherwise. Returning {} where
  // the client expects a list is what crashes the app shell (`f.filter`).
  if (/(config|tips|voice|autonudge|branding|status|themes|usage)/.test(p)) return json(route, {})
  return json(route, [])
})

// The shell gates on onboarding; skip the tour so Settings renders directly.
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
})

const assert = (cond, msg) => {
  if (cond) { console.log('  PASS  ' + msg); return true }
  console.log('  FAIL  ' + msg); fails.push(msg); return false
}

console.log('\n=== WeChat (iLink) settings panel — Playwright E2E ===\n')

// 1) Channels tab lists WeChat
await page.goto(`http://127.0.0.1:${PORT}/settings?tab=channels&channel=weixin`, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[role="listbox"]', { timeout: 15000 })
const wechatOption = page.locator('[role="option"]', { hasText: 'WeChat' }).first()
assert(await wechatOption.count() > 0, 'Channels list contains a WeChat entry')

// 2) Open it -> panel renders, not signed in
await wechatOption.click()
await page.waitForSelector('[data-testid="weixin-panel"]', { timeout: 10000 })
assert(await page.locator('[data-testid="weixin-panel"]').isVisible(), 'WeChat panel renders')
// The brand mark must actually decode — a broken asset path still renders an
// <img>, so check naturalWidth rather than mere presence.
const logoOk = await page.evaluate(() => {
  const imgs = [...document.querySelectorAll('img')].filter(i => /wechat-logo/.test(i.src))
  return imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0)
})
assert(logoOk, 'WeChat brand logo loaded (naturalWidth > 0)')
assert(
  (await page.locator('[data-testid="weixin-status"]').innerText()).includes('Not signed in'),
  'Status shows "Not signed in" before login',
)
await page.screenshot({ path: join(OUT, 'weixin-panel-initial.png') })

// 3) Start the QR login -> QR image appears
await page.locator('[data-testid="weixin-connect"]').click()
await page.waitForSelector('[data-testid="weixin-qr"] img', { timeout: 10000 })
assert(calls.start === 1, 'Connect button called qr/start exactly once')
assert(await page.locator('[data-testid="weixin-qr"] img').isVisible(), 'QR image is rendered')
assert(
  (await page.locator('[data-testid="weixin-qr"]').innerText()).includes('Waiting for scan'),
  'Shows "Waiting for scan…" while unscanned',
)
await page.screenshot({ path: join(OUT, 'weixin-panel-qr.png') })

// 4) Simulate the phone scan -> UI asks for confirmation
qrPhase = 'scaned'
await page.waitForFunction(
  () => document.querySelector('[data-testid="weixin-qr"]')?.textContent?.includes('confirm in WeChat'),
  null, { timeout: 10000 },
)
assert(true, 'Polling picked up "scaned" and asks to confirm in WeChat')
await page.screenshot({ path: join(OUT, 'weixin-panel-scanned.png') })

// 5) Confirm -> success + config refetched as connected
qrPhase = 'confirmed'
await page.waitForSelector('[data-testid="weixin-confirmed"]', { timeout: 10000 })
assert(true, 'Confirmed state renders after login completes')
await page.waitForFunction(
  () => document.querySelector('[data-testid="weixin-status"]')?.textContent?.includes('Connected'),
  null, { timeout: 10000 },
)
assert(true, 'Status flips to Connected (config was refetched)')
assert(
  (await page.locator('[data-testid="weixin-status"]').innerText()).includes('a5ace6fd482e@im.bot'),
  'Connected status shows the iLink bot account id',
)
assert(calls.status >= 2, `Status endpoint polled repeatedly (${calls.status} calls)`)
await page.screenshot({ path: join(OUT, 'weixin-panel-connected.png') })

// 6) DM policy -> allowlist reveals the editor and persists
// The picker is a Radix Select (SimpleSelect), not a native <select>: selectOption
// no longer applies — open the trigger, then click the option, which Radix portals
// to the end of <body> (so the option query is page-scoped, not field-scoped).
await page.locator('[data-testid="weixin-dm-policy"] [role="combobox"]').click()
await page.getByRole('option', { name: 'Only allowed user IDs' }).click()
await page.waitForSelector('[data-testid="weixin-allowlist"]', { timeout: 10000 })
assert(
  calls.save.some(b => b.dm_policy === 'allowlist'),
  'Changing DM policy PUT the new value to the server',
)
assert(await page.locator('[data-testid="weixin-allowlist"]').isVisible(), 'Allow-list editor appears for allowlist policy')
await page.screenshot({ path: join(OUT, 'weixin-panel-allowlist.png') })

// 7) No secret ever reaches the client
const html = await page.content()
assert(!/ilink_bot_token|WEIXIN_TOKEN/i.test(html), 'No credential material rendered in the DOM')

await browser.close()
server.close()

const real = fails.filter(f => !f.startsWith('console:') || !/favicon|404/i.test(f))
console.log(`\n=== ${real.length === 0 ? 'ALL CHECKS PASSED' : 'FAILURES: ' + real.length} ===`)
if (real.length) { real.forEach(f => console.log('  - ' + f)); process.exit(1) }
console.log(`screenshots -> ${OUT}\n`)
