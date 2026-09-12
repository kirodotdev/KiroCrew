/**
 * Screenshot harness for Settings → Security → Agent identity.
 *
 * Renders the SHIPPED section against API fixtures, one frame per posture, so
 * the PR's evidence is the real component with the real strings rather than a
 * hand-authored mock. Three states, each a state the code can actually be in:
 *
 *   off       — nothing configured; the select, its reversibility line and the
 *               hint are the whole card.
 *   dirty     — Off with an unsaved edit: Workload picked, name and Gateway URL
 *               typed, "Preview what Save grants" pressed: the Gateway and its
 *               two targets listed BEFORE Save, and the commit restating the
 *               scoped grant ("Grant 2 targets: …"); nothing persisted.
 *   workload  — configured, every catalog check green, one READY target with
 *               tools listed through the SigV4 proxy, Copy debug summary live.
 *   restart   — saved but hot-apply could not finish: the warning that a
 *               gateway relaunch is needed, plus the install-failed notice.
 *   nameless  — Workload picked with no name: the "set a workload name"
 *               warning; Save stays disarmed.
 *   nochannel — saved, but no install channel for the AgentCore extra: the
 *               notice naming the manual step.
 *   empty     — every check green, no targets and no tools yet.
 *   login     — configured for a signed-in person: tools/list skipped until a
 *               chat signs in, one target awaiting authorization, and the
 *               Gateway sign-in consent card naming the target and host.
 *   blocked   — fleet-signed ceiling: every control disabled, ownership stated.
 *   failing   — a Verify that came back red: denied control-plane call, target
 *               listing error, tools skipped.
 *
 * Builds the SPA first (SKIP_BUILD=1 to reuse dist). Same shell stubs and
 * path-routed navigation as capture-security-locales.mjs.
 *
 * Usage: node scripts/capture-agentcore-identity.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agentcore-identity'
mkdirSync(OUT, { recursive: true })

// A per-state hook run after the page settles and before the screenshot;
// kept off the fixture map so installApiFixtures never sees it.
const INTERACT = Symbol('interact')
const GATEWAY_URL = 'https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp'

const BASE = {
  '/api/security/posture': { controls: [], counts: {} },
  '/api/security/denied-commands': {
    builtins: [], user_added: [], disable_all: false,
    effective_count: 0, governance_locked: false,
  },
  '/api/governance/policy': { scopes: [], distribution: null },
  '/api/security/trusted-apps': { apps: [], ineffective: [], allowAll: false },
  '/api/tailnet/status': { enabled: false, governance_pinned: false, state: 'off' },
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
  '/api/theme/boot': { mode: 'dark', theme: '', language: 'en' },
}

const checks = (overrides = {}) => [
  'url', 'extra', 'reachable', 'ready', 'authorizer', 'url_match', 'invoke_scope', 'identity', 'tools',
].map(id => ({ id, ok: true, detail: '', ...(overrides[id] || {}) }))

const gateway = {
  id: 'GW1ABC2DEF3',
  name: 'kirocrew-demo',
  status: 'READY',
  authorizer_type: 'AWS_IAM',
  gateway_url: GATEWAY_URL,
  status_reasons: [],
}

const target = (extra = {}) => ({
  target_id: 'TGT9XYZ',
  name: 'ticketing-mcp',
  target_type: 'MCP_SERVER',
  status: 'READY',
  listing_mode: 'ALL',
  last_synchronized_at: new Date(Date.now() - 3600_000).toISOString(),
  pending_auth: false,
  authorization_url: null,
  syncable: true,
  status_reasons: [],
  ...extra,
})

const STATES = {
  off: {
    '/api/agentcore/identity': {
      configured: false, posture: null, workload_name: '', gateway_url: '',
      source: 'unset', writable: true, write_blocked: null,
      restart_required: false, extra_installed: false, extra_code: null,
    },
  },
  // Same fixtures as `off`, then the operator picks Workload without saving:
  // Save becomes pressable and the Workload name / Gateway URL fields appear.
  dirty: {
    '/api/agentcore/identity': {
      configured: false, posture: null, workload_name: '', gateway_url: '',
      source: 'unset', writable: true, write_blocked: null,
      restart_required: false, extra_installed: false, extra_code: null,
    },
    // What Save would grant, read from the DRAFTED URL before anything is
    // saved: the Gateway and its targets; tools deferred to after Save.
    '/api/agentcore/gateway/preview': {
      code: 'ok', preview: true, posture: 'workload', workload_name: '', gateway_url: GATEWAY_URL,
      gateway,
      targets: [target(), target({ target_id: 'TGT4LMB', name: 'payroll-lookup', target_type: 'LAMBDA', listing_mode: 'DEFAULT' })],
      targets_error: null,
      tools: { reachable: false, skip_reason: 'listed_after_save', via: null, items: [] },
      checks: checks().filter(c => c.id !== 'tools' && c.id !== 'identity'),
    },
    [INTERACT]: async page => {
      await page.getByRole('combobox', { name: 'Identity' }).click()
      await page.getByRole('option', { name: /^Workload/ }).click()
      await page.getByRole('textbox', { name: 'Workload name' }).fill('kirocrew-demo')
      await page.getByRole('textbox', { name: 'Gateway URL' }).fill(GATEWAY_URL)
      await page.getByRole('button', { name: 'Preview what Save grants' }).click()
      await page.getByRole('button', { name: /^Grant 2 targets: / }).waitFor()
    },
  },
  workload: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: false, extra_installed: true, extra_code: 'ok',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'ok', posture: 'workload', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway,
      targets: [target()],
      targets_error: null,
      tools: {
        reachable: true, skip_reason: null, via: 'proxy',
        items: [
          { name: 'tickets_search', description: 'Find tickets by text, owner or state' },
          { name: 'tickets_create', description: 'Open a ticket in the selected queue' },
          { name: 'tickets_comment', description: 'Append a comment to a ticket' },
        ],
      },
      checks: checks(),
    },
  },
  // Saved Workload whose hot-apply could not finish: the restart-required
  // warning and the AgentCore-support install failure notice, with the
  // catalog reporting the extra as missing.
  restart: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: true, extra_installed: false, extra_code: 'install_failed',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'extra_missing', posture: 'workload', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway: null,
      targets: [],
      targets_error: null,
      tools: { reachable: false, skip_reason: 'tools_unreachable', via: null, items: [] },
      checks: [],
    },
  },
  // Every check green on a Gateway that has no targets yet: the empty-targets
  // and empty-tools copy point at the Gateway console rather than at this page.
  // Off, then Workload picked with the name left blank: the "set a workload
  // name" warning and a Save that will not arm.
  nameless: {
    '/api/agentcore/identity': {
      configured: false, posture: null, workload_name: '', gateway_url: '',
      source: 'unset', writable: true, write_blocked: null,
      restart_required: false, extra_installed: false, extra_code: null,
    },
    [INTERACT]: async page => {
      await page.getByRole('combobox', { name: 'Identity' }).click()
      await page.getByRole('option', { name: /^Workload/ }).click()
    },
  },
  // Saved, but this install has no channel through which the AgentCore extra
  // could be installed: the notice that names the manual step.
  nochannel: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: false, extra_installed: false, extra_code: 'no_install_channel',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'extra_missing', posture: 'workload', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway: null,
      targets: [],
      targets_error: null,
      tools: { reachable: false, skip_reason: 'tools_unreachable', via: null, items: [] },
      checks: [],
    },
  },
  empty: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: false, extra_installed: true, extra_code: 'ok',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'ok', posture: 'workload', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway,
      targets: [],
      targets_error: null,
      tools: { reachable: true, skip_reason: null, via: 'proxy', items: [] },
      checks: checks(),
    },
  },
  login: {
    '/api/agentcore/identity': {
      configured: true, posture: 'login', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: false, extra_installed: true, extra_code: 'ok',
    },
    '/api/agentcore/consent': {
      pending: true,
      url: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
      host: 'login.microsoftonline.com',
      allow_source: 'builtin',
      allowlist_path: null,
    },
    '/api/agentcore/gateway': {
      code: 'ok', posture: 'login', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway: { ...gateway, authorizer_type: 'CUSTOM_JWT' },
      targets: [target({
        status: 'READY', pending_auth: true,
        authorization_url: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
      })],
      targets_error: null,
      tools: { reachable: false, skip_reason: 'login_needs_sign_in', via: null, items: [] },
      checks: checks({ tools: { ok: true, detail: 'skipped' } }),
    },
  },
  // Fleet / signed-policy users: the ceiling is read-only here, so every
  // control is disabled and the card says who owns the setting instead.
  blocked: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-fleet-07',
      gateway_url: GATEWAY_URL, source: 'policy', writable: false, write_blocked: 'signed',
      restart_required: false, extra_installed: true, extra_code: 'ok',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'ok', posture: 'workload', workload_name: 'kirocrew-fleet-07', gateway_url: GATEWAY_URL,
      gateway,
      targets: [target()],
      targets_error: null,
      tools: { reachable: true, skip_reason: null, via: 'proxy', items: [
        { name: 'tickets_search', description: 'Find tickets by text, owner or state' },
      ] },
      checks: checks(),
    },
  },
  // A Verify that did not come back green: the account cannot call the
  // Gateway control plane, so the invoke check fails with its code hint, the
  // target listing errors, and the tools list is skipped.
  failing: {
    '/api/agentcore/identity': {
      configured: true, posture: 'workload', workload_name: 'kirocrew-demo',
      gateway_url: GATEWAY_URL, source: 'policy', writable: true, write_blocked: null,
      restart_required: false, extra_installed: true, extra_code: 'ok',
    },
    '/api/agentcore/consent': { pending: false, url: null, host: null },
    '/api/agentcore/gateway': {
      code: 'ok', posture: 'workload', workload_name: 'kirocrew-demo', gateway_url: GATEWAY_URL,
      gateway,
      targets: [],
      targets_error: 'aws_denied',
      tools: { reachable: false, skip_reason: 'proxy_unavailable', via: null, items: [] },
      checks: checks({
        invoke_scope: { ok: false, detail: 'invoke_denied' },
        tools: { ok: false, detail: 'skipped' },
      }),
    },
  },
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], {
      stdio: 'inherit',
      shell: process.platform === 'win32',
    })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const [state, fixtures] of Object.entries(STATES)) {
    const context = await browser.newContext({
      viewport: { width: 1500, height: 2000 },
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    const { [INTERACT]: interact, ...routes } = fixtures
    await installApiFixtures(page, { ...BASE, ...routes })
    logPageFailures(page)
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-lang', 'en')
      localStorage.setItem('mc-yolo-ack', '1')
      window.updateAPI = {
        onState: () => () => {},
        check: async () => ({ ok: true }),
        download: async () => ({ ok: true }),
        install: async () => ({ ok: true }),
        getInfo: async () => ({
          version: '0.5.0', channel: 'stable', stampedChannel: 'stable',
          channelSwitchable: true, channelPreference: '',
          platform: 'linux-x64', packaged: true,
        }),
        setChannel: async () => ({ ok: true }),
      }
    })
    await page.goto(`${base}/settings?tab=security&section=identity`, {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForTimeout(2500)
    if (interact) {
      await interact(page)
      await page.waitForTimeout(500)
    }
    const name = `identity-${state}.png`
    await page.screenshot({ path: `${OUT}/${name}`, fullPage: true })
    console.log(name)
    await context.close()
  }
  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
