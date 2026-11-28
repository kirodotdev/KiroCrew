/**
 * Screenshot harness for #42804: the Kiro sign-in card when the sign-in stored
 * in Kiro Crew cannot be read (`kiro_crew.stored: null`).
 *
 * Runs the built SPA behind `serveDist`, every /api/** call answered from
 * fixtures by `stubDashboardApi`. No gateway, no credential.
 *
 * The frame ASSERTS the notice's text before it writes the PNG. With `--before`
 * (against a build without the fix, passed with `--dist`) it asserts the notice
 * names the failure only.
 *
 * Usage: node scripts/capture-42804-kas-vault-unreadable.mjs [outDir] [--dist <dir>] [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const BEFORE = args.includes('--before')
const distAt = args.indexOf('--dist')
const DIST = distAt >= 0 ? args[distAt + 1] : DEFAULT_DIST
const OUT = args.find((a, i) => !a.startsWith('--') && args[i - 1] !== '--dist') || '../temp-screenshots/pr-42804'
mkdirSync(OUT, { recursive: true })

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
const SOURCES = {
  active: 'unknown', kiro_crew: { stored: null }, kiro_cli: { stored: false, usable: false },
  name_pending: false, name_unreadable: false, same_account: null, can_use_kiro_cli: false,
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  let ok = true
  for (const theme of BEFORE ? ['dark'] : ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    const extra = async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KAS_CONFIG); return true }
      if (path === '/api/kas-login') { await json(route, SIGNED_OUT); return true }
      if (path === '/api/kas-login/sources') { await json(route, SOURCES); return true }
      if (path === '/api/acp-backends') { await json(route, { backends: [KAS_ROW] }); return true }
      if (path === '/api/config/schema') {
        await json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas'] }] })
        return true
      }
      if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') {
        await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' })
        return true
      }
      return false
    }
    await stubDashboardApi(page, { theme, extra })
    await page.goto(base + '/settings/kiro-account', { waitUntil: 'domcontentloaded' })
    const notice = page.getByTestId('kiro-sign-in-kiro-crew-unreadable')
    await notice.waitFor({ timeout: 20_000 })
    await page.getByTestId('kiro-sign-in-inline').waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    const text = (await notice.innerText()).replace(/\s+/g, ' ').trim()
    const hint = text.includes('Sign in again to replace it.')
    const frameOk = text.startsWith('Could not check the sign-in stored in Kiro Crew') && hint === !BEFORE
    console.log(JSON.stringify({ theme, text, frameOk }))
    ok = ok && frameOk
    const name = `${BEFORE ? '00-before' : '01-after'}-${theme}`
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
    await context.close()
  }
  await browser.close()
  srv.close()
  if (!ok) {
    console.error('FAIL: the notice did not read as expected')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
