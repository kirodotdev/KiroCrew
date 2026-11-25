/**
 * A walkthrough video of Settings > Kiro account from a built SPA with every
 * /api call stubbed: signed out (the pointer circles the ghost so its eyes can be
 * seen following it), Continue with Google, the code step, Copy link, Use
 * another way back to the choices, the code step again, then the sign-in lands:
 * placeholders, then the name and the Credits card.
 *
 * Usage: [PHONE=1] node scripts/record-kiro-account.mjs [outDir]
 * Writes one .webm into outDir and prints its path and the step times.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

delete process.env.LD_LIBRARY_PATH
const OUT = process.argv[2] || '../temp-screenshots/kiro-account-video'
mkdirSync(OUT, { recursive: true })

const KAS_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } }
const SIGNED_IN = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true,
  refresh_rejected: false, usable: true,
}
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
const KAS_ROW = {
  id: 'kas', policy_id: 'kas', selectable: true, independent_setup: false,
  installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  capabilities: [{ id: 'crew_tools', available: true }],
  security_notes: [], operator_notes: [], tool_approval: 'agent_spec', offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
}

const { srv, base } = await serveDist(DEFAULT_DIST)
const browser = await chromium.launch()
const PHONE = process.env.PHONE === '1'
const VP = PHONE ? { width: 390, height: 844 } : { width: 1280, height: 800 }
const context = await browser.newContext({
  viewport: VP, deviceScaleFactor: PHONE ? 2 : 1, hasTouch: PHONE, isMobile: PHONE,
  recordVideo: { dir: OUT, size: VP },
})
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
const page = await context.newPage()
logPageProblems(page)
let signedIn = false
const extra = async (path, route) => {
  if (path === '/api/config/kirocrew') { await json(route, KAS_CONFIG); return true }
  if (path === '/api/kas-login') { await json(route, signedIn ? SIGNED_IN : SIGNED_OUT); return true }
  // Sign-in landing POSTs a refresh; the real gateway takes about a second.
  if (path === '/api/sessions/usage/refresh') {
    await new Promise(r => setTimeout(r, 1200))
    await json(route, signedIn ? READING : NO_READING); return true
  }
  if (path === '/api/sessions/usage') { await json(route, signedIn ? READING : NO_READING); return true }
  if (path === '/api/kas-login/sources') {
    await json(route, {
      active: signedIn ? 'kiro_crew' : 'none', kiro_crew: { stored: signedIn }, kiro_cli: { stored: false },
      name_pending: false, name_unreadable: false, same_account: null, can_use_kiro_cli: false,
    })
    return true
  }
  if (path === '/api/kas-login/loopback') { await json(route, { error: 'loopback_unavailable', code: 'loopback_unavailable' }, 409); return true }
  if (path === '/api/kas-login/device') {
    await json(route, { login_id: 'lid-1', user_code: 'WXYZ-1234', verification_uri_complete: 'https://view.awsapps.com/start/#/device?user_code=WXYZ-1234', expires_at: '2099-01-01T00:00:00Z' })
    return true
  }
  if (path === '/api/kas-login/poll') { await json(route, { status: signedIn ? 'authorized' : 'pending' }); return true }
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
// A visible cursor dot, so the video shows where each click lands.
await page.addInitScript(() => {
  addEventListener('DOMContentLoaded', () => {
    const d = document.createElement('div')
    d.style.cssText = 'position:fixed;z-index:99999;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:rgba(124,58,237,.35);border:2px solid rgba(124,58,237,.9);pointer-events:none;left:-50px;top:-50px;transition:transform .12s'
    document.body.appendChild(d)
    addEventListener('mousemove', e => { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px' }, true)
    addEventListener('mousedown', () => { d.style.transform = 'scale(.7)' }, true)
    addEventListener('mouseup', () => { d.style.transform = '' }, true)
  })
})
const pause = ms => page.waitForTimeout(ms)
async function clickSlow(locator) {
  if (PHONE) { await pause(500); await locator.tap(); return }
  const b = await locator.boundingBox()
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 25 })
  await pause(400)
  await locator.click()
}
const steps = []
const mark = label => steps.push({ label, t: Date.now() })
const t0 = Date.now()

await page.goto(base + '/settings/kiro-account', { waitUntil: 'domcontentloaded' })
await page.getByRole('button', { name: 'Continue with Google' }).first().waitFor({ timeout: 20_000 })
mark('1 signed out'); await pause(PHONE ? 2000 : 800)
// Circle the pointer around the ghost so its eyes can be seen following it.
for (const [x, y] of PHONE ? [] : [[300, 150], [1200, 150], [1200, 700], [300, 700], [780, 600]]) {
  await page.mouse.move(x, y, { steps: 30 }); await pause(350)
  const tf = await page.getByTestId('looking-ghost-eyes').evaluate(img => img.style.transform)
  console.log('eyes at', x, y, '->', tf || '(none)')
}
await clickSlow(page.getByRole('button', { name: 'Continue with Google' }).first())
await page.getByTestId('kas-login-user-code').waitFor()
mark('2 code step'); await pause(2500)
await clickSlow(page.getByTestId('kas-login-copy-link-text'))
mark('3 copy link'); await pause(1500)
await clickSlow(page.getByTestId('kas-login-back-to-options'))
await page.getByRole('button', { name: 'Continue with GitHub' }).waitFor()
mark('4 back to options'); await pause(2000)
await clickSlow(page.getByRole('button', { name: 'Continue with Google' }).first())
await page.getByTestId('kas-login-user-code').waitFor()
mark('5 code step again'); await pause(2000)
signedIn = true
await page.getByText('Switch account').waitFor({ timeout: 30_000 })
mark('6 signed in, loading')
await page.getByText('412', { exact: true }).waitFor({ timeout: 10_000 })
mark('7 name and credits in'); await pause(3000)

const video = page.video()
await context.close()
await browser.close()
srv.close()
console.log('video', await video.path())
for (const s of steps) console.log(((s.t - t0) / 1000).toFixed(1) + 's', s.label)
