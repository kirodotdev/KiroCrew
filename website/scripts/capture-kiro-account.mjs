/**
 * Full-page shots of Settings > Kiro account, one per sign-in state, desktop and
 * phone, plus the chat sign-in dialog's code step (the same code screen), from a
 * built SPA with every /api call stubbed.
 *
 *   01 signed out                 the ghost, "Sign in to Kiro", the choices; no Credits
 *   02 code step                  "Finish in your browser" header, Use another way,
 *                                 Open sign-in page, Copy link, the code; no copy-code
 *   03 signed in, loading         gray avatar and name, the actions right away
 *   04 signed in                  initials, blurred e-mail with its eye, Credits
 *   05 signed in, e-mail shown    the eye pressed
 *   06 signed in, no balance yet  one gray line with a small Refresh
 *   07 sign-in expired            the warning header with Sign in again; no Credits
 *   08 dialog code step           Switch account -> the dialog keeps its own
 *                                 "Finish in your browser" heading and hint
 *   09 kiro-cli signed in         Crew signed out, sessions on kiro-cli's login:
 *                                 the header names the kiro-cli account, the
 *                                 sign-in choices in place, Credits without a name
 *   10 signed in, no refresh      the one muted "cannot renew itself" line
 *   11-17                         01, 02, 03, 04, 06, 08 and 09 on a 390 px phone
 *
 * After the change (default) each frame asserts what must and must not show
 * before it writes the PNG, and the run exits 1 on any miss. `--before` shoots
 * the same frames from a build of main with no assertions, for the pairs.
 *
 * Usage: node scripts/capture-kiro-account.mjs [outDir] [--dist <dir>] [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// A Playwright-bundled Chromium fails on the system library path some hosts set.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const BEFORE = args.includes('--before')
const distAt = args.indexOf('--dist')
const DIST = distAt >= 0 ? args[distAt + 1] : DEFAULT_DIST
const OUT = args.find((a, i) => !a.startsWith('--') && args[i - 1] !== '--dist') || '../temp-screenshots/kiro-account'
mkdirSync(OUT, { recursive: true })

const KAS_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } }
const SIGNED_IN = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true,
  refresh_rejected: false, usable: true,
}
const EXPIRED = { ...SIGNED_IN, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
const SIGNED_OUT = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}
const READING = {
  usage: {
    credits_plan: 1000, credits_used: 412, plan: 'Kiro Pro', resets: '2026-12-01',
    email: 'nina@example.com', account_type: 'Google',
  },
}
const NO_READING = { usage: { available: false } }
/** The usage read never answers: the page shows what it draws while it waits. */
const PENDING = Symbol('pending')
const KAS_ROW = {
  id: 'kas', policy_id: 'kas', selectable: true, independent_setup: false,
  installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  capabilities: [{ id: 'crew_tools', available: true }],
  security_notes: [], operator_notes: [], tool_approval: 'agent_spec', offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const failures = []

const KIRO_CLI_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kiro' } }
const NO_REFRESH = { ...SIGNED_IN, has_refresh_token: false }

async function frame(name, { status, usage, config = KAS_CONFIG, cliSignedIn = false, signIn = false, dialog = false, revealEmail = false, want = [], absent = [], phone = false, check }) {
  const viewport = phone ? { width: 390, height: 844 } : { width: 1280, height: 900 }
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2, hasTouch: phone, isMobile: phone })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    if (path === '/api/config/kirocrew') { await json(route, config); return true }
    if (path === '/api/kas-login') { await json(route, status); return true }
    if (path === '/api/sessions/usage') {
      if (usage === PENDING) return true // left unanswered on purpose
      await json(route, usage)
      return true
    }
    if (path === '/api/kas-login/sources') {
      await json(route, {
        active: status.authenticated ? 'kiro_crew' : cliSignedIn ? 'kiro_cli' : 'none',
        kiro_crew: { stored: status.authenticated },
        kiro_cli: cliSignedIn
          ? { stored: true, usable: true, email: 'nina@example.com', account_type: 'Google' }
          : { stored: false },
        name_pending: false, name_unreadable: false, same_account: null, can_use_kiro_cli: cliSignedIn,
      })
      return true
    }
    if (path === '/api/kas-login/loopback') { await json(route, { error: 'loopback_unavailable', code: 'loopback_unavailable' }, 409); return true }
    if (path === '/api/kas-login/device') {
      await json(route, { login_id: 'lid-1', user_code: 'WXYZ-1234', verification_uri_complete: 'https://view.awsapps.com/start/#/device?user_code=WXYZ-1234', expires_at: '2099-01-01T00:00:00Z' })
      return true
    }
    if (path === '/api/kas-login/poll') { await json(route, { status: 'pending' }); return true }
    if (path === '/api/kas-login/cancel') { await json(route, { ok: true }); return true }
    if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') {
      await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' })
      return true
    }
    if (path === '/api/acp-backends') { await json(route, { backends: [KAS_ROW] }); return true }
    if (path === '/api/config/schema') {
      await json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas'] }] })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { theme: 'light', extra })
  await page.goto(base + '/settings/kiro-account', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('kiro-sign-in-card').waitFor({ timeout: 20_000 })
  await page.waitForTimeout(2500)
  if (dialog) {
    await page.getByRole('button', { name: 'Switch account' }).click()
    await page.getByTestId('kiro-sign-in-dialog').waitFor({ timeout: 10_000 })
  }
  const scope = dialog ? page.getByTestId('kiro-sign-in-dialog') : page
  if (signIn) {
    await scope.getByRole('button', { name: 'Continue with Google' }).first().click()
    await scope.getByTestId('kas-login-user-code').waitFor({ timeout: 10_000 })
    await page.waitForTimeout(500)
  }
  if (revealEmail) {
    await page.getByRole('button', { name: 'Show email' }).first().click()
    await page.waitForTimeout(300)
  }
  if (!BEFORE) {
    const text = await (dialog ? scope : page.locator('main')).innerText()
    for (const w of want) if (!text.includes(w)) failures.push(`${name}: missing "${w}"`)
    for (const a of absent) if (text.includes(a)) failures.push(`${name}: should not show "${a}"`)
    if (check) {
      const problem = await check(page, scope)
      if (problem) failures.push(`${name}: ${problem}`)
    }
  }
  if (!dialog) {
    // The Settings pane scrolls inside the page, so `fullPage` alone stops at
    // the window: grow the window until the pane no longer overflows.
    const overflow = await page.evaluate(() => Math.max(0, ...[...document.querySelectorAll('main, main *')]
      .filter(el => el.scrollHeight > el.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(el).overflowY))
      .map(el => el.scrollHeight - el.clientHeight)))
    if (overflow > 0) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height + overflow })
      await page.waitForTimeout(200)
    }
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: !dialog })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

// The options sit in a narrow centered column, not the full pane.
const narrowOptions = async page => {
  const box = await page.getByRole('button', { name: 'Continue with Google' }).first().boundingBox()
  return box && box.width > 330 ? `option button is ${Math.round(box.width)}px wide` : null
}
const ghostClipped = async page => {
  const g = page.getByTestId('looking-ghost')
  if (!(await g.count())) return 'ghost missing'
  // Two image layers (body, eyes) in a round box that clips them; no inline svg.
  return g.evaluate(el => {
    const s = getComputedStyle(el)
    if (el.querySelector('svg')) return 'ghost is an inline svg'
    if (el.querySelectorAll('img').length !== 2) return 'ghost is not two image layers'
    if (s.overflow !== 'hidden' || !s.borderRadius || s.borderRadius === '0px') return 'ghost not clipped to its disc'
    return [...el.querySelectorAll('img')].every(i => i.complete && i.naturalWidth > 0) ? null : 'a ghost layer did not load'
  })
}
// The Settings code step: one heading (the page's), Use another way just above
// a full-width button, Copy link under it, the code box the button's width.
const settingsCodeView = async page => {
  if (await page.getByRole('button', { name: /copy code/i }).count()) return 'code still has a copy button'
  if ((await page.getByRole('heading', { name: 'Finish in your browser' }).count()) !== 1) return 'not exactly one "Finish in your browser" heading'
  const btn = await page.getByTestId('kas-login-verification-link').boundingBox()
  const box = await page.getByTestId('kas-login-user-code').locator('..').boundingBox()
  const back = await page.getByTestId('kas-login-back-to-options').boundingBox()
  const pane = await page.getByTestId('kiro-account-centered').boundingBox()
  if (!btn || !box || !back || !pane) return 'a part is missing'
  if (Math.abs(btn.width - box.width) > 2) return `button ${Math.round(btn.width)}px vs code box ${Math.round(box.width)}px`
  const mid = pane.x + pane.width / 2
  for (const [n, r] of [['button', btn], ['code box', box]]) {
    if (Math.abs(r.x + r.width / 2 - mid) > 3) return `${n} off center`
  }
  const copyLink = await page.getByTestId('kas-login-copy-link-text').boundingBox()
  if (!copyLink || copyLink.y < btn.y + btn.height || copyLink.y + copyLink.height > box.y) return 'Copy link is not between the button and the code'
  if (back.y > btn.y || btn.y - back.y > 60 || Math.abs(back.x - btn.x) > 6) return `Use another way is not just above the button (dy ${Math.round(btn.y - back.y)}, dx ${Math.round(back.x - btn.x)})`
  return null
}
// The dialog's code step: the same screen, keeping its own heading and hint.
const dialogCodeView = async (_page, dialog) => {
  if (await dialog.getByRole('button', { name: /copy code/i }).count()) return 'code still has a copy button'
  if (!(await dialog.getByRole('heading', { name: 'Finish in your browser' }).count())) return 'dialog lost its "Finish in your browser" heading'
  if (!(await dialog.getByTestId('kas-login-code-heading').count())) return 'dialog lost its hint'
  if (!(await dialog.getByRole('button', { name: 'Use a different sign-in' }).count())) return 'no back arrow in the dialog header'
  return null
}

// Crew signed out, sessions on kiro-cli's login: the hero names that account
// (its e-mail toggle), the sign-in choices show in place, and Credits does not
// name the account a second time.
const kiroCliHeaderView = async page => {
  const hero = page.getByTestId('kiro-sign-in-hero')
  if ((await hero.getAttribute('data-hero')) !== 'kiro-cli') return 'hero is not the kiro-cli account header'
  if (!(await hero.getByTestId('kiro-account-email-toggle').count())) return 'no e-mail toggle in the header'
  if (await page.getByTestId('kiro-credits-card').getByRole('button', { name: 'Show email' }).count()) return 'Credits names the kiro-cli account again'
  return null
}

const SIGNED_OUT_ABSENT = ['Credits', 'Refresh', 'Manage account']
const shots = [
  ['01-signed-out', { status: SIGNED_OUT, usage: NO_READING, want: ['Sign in to Kiro', 'Your agents run on your Kiro account.', 'Continue with Google'], absent: SIGNED_OUT_ABSENT, check: async page => (await ghostClipped(page)) || narrowOptions(page) }],
  ['02-code-step', { status: SIGNED_OUT, usage: NO_READING, signIn: true, want: ['Use another way', 'Copy link', 'Finish in your browser', 'Open the page, then check it shows this code.', 'WXYZ-1234'], absent: SIGNED_OUT_ABSENT, check: settingsCodeView }],
  ['03-signed-in-loading', { status: SIGNED_IN, usage: PENDING, want: ['Signed in with Google', 'Switch account', 'Sign out'], absent: ['Continue with Google'], check: async page => (await page.getByTestId('kiro-account-name-placeholder').count()) ? null : 'no name placeholder' }],
  ['04-signed-in', { status: SIGNED_IN, usage: READING, want: ['Signed in with Google', 'Switch account', 'Sign out', 'Credits', '412'], absent: ['Continue with Google', 'Account details unavailable'], check: async page => (await page.getByTestId('kiro-account-email-toggle').count()) ? null : 'no show-email button' }],
  ['05-signed-in-email-shown', { status: SIGNED_IN, usage: READING, revealEmail: true, want: ['nina@example.com', 'Signed in with Google'] }],
  ['06-signed-in-no-balance', { status: SIGNED_IN, usage: NO_READING, want: ['Signed in with Google', 'Your balance isn’t ready yet.', 'Refresh'], absent: ['Account details unavailable', 'No balance reading'] }],
  ['07-sign-in-expired', { status: EXPIRED, usage: NO_READING, want: ['Sign-in expired.', 'Sign in again', 'Sign out'], absent: ['Switch account', 'Credits', 'Your balance isn’t ready yet.'] }],
  ['08-dialog-code-step', { status: SIGNED_IN, usage: READING, dialog: true, signIn: true, want: ['Finish in your browser', 'Check the page shows this code', 'Copy link', 'WXYZ-1234'], check: dialogCodeView }],
  ['09-kiro-cli-signed-in', { status: SIGNED_OUT, usage: READING, config: KIRO_CLI_CONFIG, cliSignedIn: true, want: ['Using your Kiro CLI sign-in', 'Continue with Google', 'Credits', '412'], check: kiroCliHeaderView }],
  ['10-signed-in-no-refresh', { status: NO_REFRESH, usage: READING, want: ['cannot renew itself', 'Access token expires', 'Credits'] }],
]
for (const [name, opts] of shots) await frame(name, opts)
const PHONE = { '01-signed-out': '11', '02-code-step': '12', '03-signed-in-loading': '13', '04-signed-in': '14', '06-signed-in-no-balance': '15', '08-dialog-code-step': '16', '09-kiro-cli-signed-in': '17' }
for (const [name, opts] of shots) {
  if (!PHONE[name]) continue
  // Width checks are desktop-only: on a phone the column fills the screen.
  const { check, ...rest } = opts
  const keep = check === settingsCodeView || check === dialogCodeView || check === kiroCliHeaderView ? check : undefined
  await frame(`${PHONE[name]}-phone-${name.slice(3)}`, { ...rest, check: keep, phone: true })
}

await browser.close()
srv.close()
if (failures.length) { console.error(failures.join('\n')); process.exit(1) }
console.log('all frames ok ->', OUT)
