/**
 * Capture Settings → Agent Harness for PR #13888: the coding-agent switch moved
 * off the Developer page (Developer Mode only) into ordinary Settings, and was
 * then redesigned to mirror first-run setup's "Use other coding agents" picker
 * (radio rows, detail under the checked row, Kiro sign-in inside the KAS row).
 *
 * Usage: node scripts/capture-13888-agent-harness.mjs [base-url] [out-dir] [shot-name]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 * Every /api/ call is answered from the fixtures below; no gateway is involved.
 *
 * Scenes: Claude Code in use (light + dark), Codex checked and missing, KAS
 * checked with the compact Kiro sign-in, and a harness that needs a re-check
 * after an install the gateway cached as absent.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../.github/screenshots/pr-13888'
mkdirSync(out, { recursive: true })

/** The capability card the gateway projects, trimmed to what a screenshot needs. */
const card = (available, extra = {}) => ({
  capabilities: [
    { id: 'crew_tools', available: true },
    { id: 'member_thread_tools', available: true },
    { id: 'mid_turn_steer', available },
    { id: 'model_switch', available },
    { id: 'manual_compact', available: false, measured: false, unmeasured_reason: 'no_driven_capture' },
  ],
  security_notes: [],
  operator_notes: [],
  tool_approval: 'agent_spec',
  offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
  ...extra,
})

const probe = (id, extra) => ({
  id, policy_id: id || 'kiro', selectable: true, independent_setup: id !== '' && id !== 'kas',
  installed: 'installed', missing_components: [], install_command: '',
  restart_required: false, ...card(true), ...extra,
})

const backends = [
  probe(''),
  probe('kas', {
    ...card(true, { tool_approval: 'seeded_settings', security_notes: ['host_credential_to_child'] }),
  }),
  probe('claude', {
    ...card(false, {
      tool_approval: 'session_config',
      security_notes: ['crew_sandbox_stands_down'],
      operator_notes: ['own_credential_store'],
      mcp: { per_tool_deny: 'whole-server', costs_whole_server: true, ineffective: ['hooks', 'permission_mode'] },
    }),
  }),
  probe('codex', {
    installed: 'missing', missing_components: ['codex-acp'],
    install_command: 'npm install -g @zed-industries/codex-acp',
    ...card(false),
  }),
  probe('opencode', { ...card(false) }),
  probe('pi', { restart_required: true, ...card(false) }),
  probe('goose', { installed: 'unknown', ...card(false) }),
  probe('deepseek', {
    selectable: false,
    ...card(false, { offered_by_build: false, tool_approval: 'unverified' }),
  }),
]

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

const shots = [
  { name: 'settings-agent-harness', theme: 'light', current: 'claude' },
  { name: 'settings-agent-harness-dark', theme: 'dark', current: 'claude' },
  { name: 'settings-agent-harness-codex-missing', theme: 'light', current: 'claude', pick: 'codex' },
  { name: 'settings-agent-harness-kas-sign-in', theme: 'dark', current: 'claude', pick: 'KAS (kiro-agent)' },
  { name: 'settings-agent-harness-restart-needed', theme: 'light', current: 'claude', pick: 'pi' },
  // KAS users read token expiry on Overview, so it points them at the new card.
  { name: 'overview-kiro-sign-in-moved', theme: 'dark', current: 'kas', path: '/overview', ready: 'kiro-sign-in-moved', crop: true },
]

const browser = await chromium.launch()
try {
  for (const s of shots) {
    if (process.argv[4] && process.argv[4] !== s.name) continue
    const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 1.4 })
    const errors = []
    page.on('pageerror', error => errors.push(String(error)))
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const p = new URL(route.request().url()).pathname
      if (p === '/api/config/kirocrew') return json(route, { agent: { acp_backend: s.current } })
      if (p === '/api/config/schema') return json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas', 'claude', 'codex', 'opencode', 'pi', 'goose'] }] })
      if (p === '/api/acp-backends') return json(route, { backends })
      if (p === '/api/kas-login') return json(route, signedOut)
      return json(route, {})
    })
    await page.goto(`${base}/capture/settings-agent-harness.html?theme=${s.theme}&path=${encodeURIComponent(s.path ?? '/settings/agent')}`)
    if (s.ready) await page.getByTestId(s.ready).waitFor({ timeout: 20000 })
    else await page.getByRole('radio', { name: 'Claude Code' }).waitFor({ timeout: 20000 })
    if (s.pick) {
      await page.getByRole('radio', { name: s.pick }).check()
      await page.getByTestId('agent-harness-detail').waitFor()
    }
    await page.waitForTimeout(800)
    // Crop to the stat row and the signpost: the rest of Overview renders
    // against empty fixtures, and its placeholder errors are not this change.
    const clip = s.crop ? await page.getByTestId(s.ready).boundingBox() : null
    await page.screenshot({
      path: `${out}/${s.name}.png`,
      animations: 'disabled',
      ...(clip ? { clip: { x: 0, y: clip.y - 130, width: 1360, height: clip.height + 160 } } : {}),
    })
    console.log(`${s.name}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
} finally {
  await browser.close()
}
