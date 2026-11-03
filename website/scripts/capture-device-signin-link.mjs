/**
 * Screenshot harness for the device sign-in step's verification link.
 *
 * Runs the REAL built SPA behind `serveDist`, every /api/** call answered from
 * fixtures by `stubDashboardApi`. No gateway and no credential: the Kiro sign-in
 * card on Settings > Agent Harness is signed out, "Continue with Google" begins a
 * device login whose answer is a fixture, and the poll stays pending.
 *
 * Each frame ASSERTS the link before it writes the PNG, so a stale bundle fails
 * loudly instead of producing a plausible picture:
 *
 *   01 dark   -> the URL is a link opening in a new tab, beside a copy button
 *   02 light  -> the same, light theme
 *   03 copied -> the copy button after a click reads "Copied"
 *
 * Usage: node scripts/capture-device-signin-link.mjs [outDir] [--dist <dir>] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const distAt = args.indexOf('--dist')
const DIST = distAt >= 0 ? args[distAt + 1] : DEFAULT_DIST
const OUT = args.find((a, i) => !a.startsWith('--') && args[i - 1] !== '--dist') || '../temp-screenshots/device-signin-link'
if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const URL_FIXTURE = 'https://app.kiro.dev/account/device?user_code=WXYZ-1234'
const KAS_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } }
const SIGNED_OUT = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}
const KAS_ROW = {
  id: 'kas', policy_id: 'kas', selectable: true, independent_setup: false,
  installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  capabilities: [{ id: 'crew_tools', available: true }],
  security_notes: [], operator_notes: [], tool_approval: 'agent_spec', offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const results = []

  async function frame(name, { theme, copy = false }) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
    const page = await context.newPage()
    logPageProblems(page)
    const extra = async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KAS_CONFIG); return true }
      if (path === '/api/kas-login') { await json(route, SIGNED_OUT); return true }
      if (path === '/api/kas-login/device') {
        await json(route, { login_id: 'lid-1', user_code: 'WXYZ-1234', verification_uri_complete: URL_FIXTURE, expires_at: '2099-01-01T00:00:00Z' })
        return true
      }
      if (path === '/api/kas-login/poll') { await json(route, { status: 'pending' }); return true }
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
    await stubDashboardApi(page, { slots: [], theme, extra })
    await page.goto(base + '/settings/agent', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Continue with Google' }).first().click({ timeout: 20_000 })
    const link = page.getByTestId('kas-login-verification-link')
    await link.waitFor({ timeout: 20_000 })
    const copyButton = page.getByRole('button', { name: copy ? 'Copied' : 'Copy link' })
    if (copy) {
      await page.getByRole('button', { name: 'Copy link' }).click()
      await copyButton.waitFor({ timeout: 5_000 }).catch(() => {})
    }
    const got = {
      href: await link.getAttribute('href'),
      target: await link.getAttribute('target'),
      rel: await link.getAttribute('rel'),
      copyButton: await copyButton.count(),
      hint: await page.getByText('Click to copy').count(),
    }
    const ok = got.href === URL_FIXTURE && got.target === '_blank' && got.rel === 'noopener noreferrer'
      && got.copyButton === 1 && got.hint === 0
    results.push({ name, ok, got })
    if (!VERIFY_ONLY) {
      // The device step's two numbered rows, with a margin.
      const steps = page.locator('ol').filter({ has: link })
      const box = await steps.boundingBox()
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: Math.max(0, box.x - 24), y: Math.max(0, box.y - 24), width: box.width + 48, height: box.height + 48 },
      })
      console.log('wrote', `${OUT}/${name}.png`)
    }
    await context.close()
  }

  await frame('01-link-dark', { theme: 'dark' })
  await frame('02-link-light', { theme: 'light' })
  await frame('03-copied-dark', { theme: 'dark', copy: true })

  await browser.close()
  srv.close()
  for (const r of results) console.log(r.ok ? 'PASS' : 'FAIL', r.name, JSON.stringify(r.got))
  if (results.some(r => !r.ok)) process.exit(1)
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
