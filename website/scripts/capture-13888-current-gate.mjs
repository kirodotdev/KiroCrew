/**
 * Render the production first-run gate at every review state for PR #13888.
 *
 * Usage: node scripts/capture-13888-current-gate.mjs [base-url] [out-dir] [scenario]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 * Every /api/ call is answered from the fixtures below; no gateway is involved.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../.github/screenshots/pr-13888'
mkdirSync(out, { recursive: true })

const status = {
  platform: 'Linux', installed: false, authenticated: false, ready: false,
  initial_setup_complete: false, repair_required: false, bundled_cli: false,
  docs_url: 'https://kiro.dev/cli/', login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true, sandbox_unavailable: false, sandbox_backend_available: true,
  sandbox_failure_kind: '', sandbox_detail: '', sandbox_remedy: '',
  missing_agent_specs: [], agent_spec_repair_error: '',
}
const probe = (id, extra) => ({
  id, policy_id: id, selectable: true, independent_setup: true,
  installed: 'installed', missing_components: [], install_command: '',
  restart_required: false, ...extra,
})
const claudeReady = probe('claude')
const claudeRestart = probe('claude', { restart_required: true })
const codexMissing = probe('codex', {
  installed: 'missing', missing_components: ['codex-acp'],
  install_command: 'npm install -g @zed-industries/codex-acp',
})
const codexInstalled = probe('codex')

/** scenario -> fixtures and the interaction that reaches the state. */
const scenarios = {
  'kiro-missing': {},
  'kiro-signed-out': { status: { installed: true } },
  windows: { status: { platform: 'Windows' } },
  // Native Windows: no OS sandbox, but unsandboxed execution is the platform
  // default, so a non-enforced harness can still finish setup.
  'windows-claude-ready': {
    status: {
      platform: 'Windows', sandbox_backend_available: false,
      unsandboxed_exec_permitted: true, sandbox_blocked_backends: ['codex'],
    },
    backends: [claudeReady, codexMissing], expand: true, pick: 'Claude Code',
    act: async page => {
      const use = page.getByTestId('other-agent-detail').getByRole('button', { name: /Use Claude Code/ })
      await use.waitFor()
      if (!(await use.isEnabled())) throw new Error('Claude Code is not usable on native Windows')
    },
  },
  unknown: { status: { platform: '' } },
  configerror: { configCode: 500 },
  'codex-missing': { backends: [claudeReady, codexMissing], expand: true, pick: 'codex' },
  'claude-restart': { backends: [claudeRestart, codexMissing], expand: true, pick: 'Claude Code' },
  'claude-ready': { backends: [claudeReady, codexMissing], expand: true, pick: 'Claude Code' },
  'claude-unverified': {
    backends: [probe('claude', { installed: 'unknown' }), codexMissing], expand: true, pick: 'Claude Code',
  },
  'switch-failure': {
    backends: [claudeReady, codexMissing], expand: true, pick: 'Claude Code', patchCode: 500,
    act: async page => {
      await page.getByRole('button', { name: /Use Claude Code/ }).click()
      await page.getByTestId('other-agent-switch-error').waitFor()
    },
  },
  'sandbox-blocked': {
    // Host verdict: no sandbox backend on this host, so Check again can help.
    status: { sandbox_backend_available: false, sandbox_blocked_backends: ['codex'] },
    config: { acp_backend: 'codex' },
    backends: [codexInstalled],
    act: async page => {
      const detail = page.getByTestId('other-agent-detail')
      await detail.getByRole('button', { name: 'Check again', exact: true }).waitFor()
      if (await detail.getByRole('button', { name: /Use codex/ }).isEnabled()) {
        throw new Error('Sandbox-blocked agent action is enabled')
      }
    },
  },
  'sandbox-off': {
    // The host has a sandbox, but agent.sandbox turned it off: name the setting,
    // and offer no Check again (pressing it cannot change the verdict).
    status: { sandbox_blocked_backends: ['codex'] }, config: { acp_backend: 'codex' },
    backends: [codexInstalled],
    act: async page => {
      const detail = page.getByTestId('other-agent-detail')
      await detail.getByText(/agent\.sandbox/).waitFor()
      if (await detail.getByRole('button', { name: 'Check again', exact: true }).count()) {
        throw new Error('Sandbox-off detail still offers Check again')
      }
    },
  },
  // Signed out AND too old: the update card must not claim a sign-in.
  outdated: { status: { installed: true, authenticated: false, acp_supported: false } },
  'kiro-switch-failure': {
    config: { acp_backend: 'claude' }, patchCode: 500,
    backends: [probe('claude', { installed: 'missing', missing_components: ['claude'],
      install_command: 'npm install -g @zed-industries/claude-code-acp' })],
    act: async page => {
      await page.getByRole('button', { name: 'Use Kiro CLI instead' }).click()
      await page.getByTestId('use-kiro-instead-error').waitFor()
      await page.getByTestId('use-kiro-instead-error').scrollIntoViewIfNeeded()
    },
  },
  // The gateway answered, but this build offers no harness besides Kiro CLI.
  'other-agents-none': {
    backends: [], expand: true,
    act: page => page.getByText('This build offers no other coding agents.').waitFor(),
  },
  'probe-failure': {
    backendsCode: 503, expand: true,
    act: page => page.getByTestId('other-agents-probe-error').getByRole('button', { name: 'Try again' }).waitFor(),
  },
  // The configured agent is ready, so the dashboard opens; the automatic
  // marker write (the recheck POST) answers 503 and the notice floats over it.
  'marker-write-failure': {
    config: { acp_backend: 'claude' }, backends: [claudeReady],
    act: page => page.getByTestId('kiro-gate-marker-write-error-region').getByRole('button', { name: /Try again|Retry/ }).waitFor(),
  },
  'recheck-failure': {
    backends: [probe('claude', { installed: 'missing', missing_components: ['claude'],
      install_command: 'npm install -g @zed-industries/claude-code-acp' })],
    expand: true, pick: 'Claude Code',
    act: async page => {
      await page.getByTestId('other-agent-detail').getByRole('button', { name: 'Check again', exact: true }).click()
      await page.getByTestId('other-agent-recheck-error').waitFor()
    },
  },
}

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})
const failure = { error: 'Unavailable', code: 'probe_unavailable' }

const browser = await chromium.launch()
try {
  for (const [name, s] of Object.entries(scenarios)) {
    if (process.argv[4] && process.argv[4] !== name) continue
    const page = await browser.newPage({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 1.5 })
    page.on('pageerror', error => { throw error })
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      const method = route.request().method()
      if (path === '/api/kiro-prerequisite') return json(route, { ...status, ...s.status })
      if (path === '/api/config/kirocrew') {
        if (method === 'PATCH') return json(route, failure, s.patchCode ?? 200)
        return s.configCode ? json(route, failure, s.configCode) : json(route, { agent: s.config ?? {} })
      }
      if (path === '/api/acp-backends') {
        return s.backendsCode
          ? json(route, failure, s.backendsCode)
          : json(route, { backends: s.backends ?? [claudeReady, codexMissing] })
      }
      if (path === '/api/acp-backends/recheck') return json(route, failure, 503)
      return json(route, {})
    })
    await page.goto(`${base}/capture/setup-check-probe-error.html?theme=dark`)
    if (s.expand) await page.getByRole('button', { name: 'Use other coding agents' }).click()
    if (s.pick) await page.getByRole('radio', { name: s.pick }).check()
    if (s.act) await s.act(page)
    else await page.getByRole('heading').first().waitFor()
    const detail = page.getByTestId('other-agent-detail')
    if (await detail.count()) await detail.scrollIntoViewIfNeeded()
    // Freeze CSS transitions: the fold chevron rotates on open and a mid-turn frame reads as a broken glyph.
    await page.screenshot({ path: `${out}/${name}.png`, animations: "disabled" })
    console.log(`${name}: captured`)
    await page.close()
  }
} finally {
  await browser.close()
}
