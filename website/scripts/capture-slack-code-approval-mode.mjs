/**
 * Screenshot harness for the code-channel approval mode on Settings → Slack →
 * Slack Code → Code channels.
 *
 * Two states a reviewer needs to judge: the default (Normal, no warning, the
 * preview's Approvals row) and Trust picked (the inline warning, the preview
 * reading Trust · Set here). Every frame is written only after its assertions
 * pass, so a wrong frame fails the run instead of shipping as evidence.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures by Playwright. No gateway, no dashboard token, no data written.
 *
 * Usage:
 *   npm run build && npx vite preview --port 6813 &
 *   node scripts/capture-slack-code-approval-mode.mjs http://127.0.0.1:6813 \
 *     ../temp-screenshots/slack-code-approval-mode
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/slack-code-approval-mode'

mkdirSync(OUT, { recursive: true })

const scene = { theme: 'dark', approval: null }

const slackConfig = () => ({
  connected: true, connect_error: '', configured: true, read_only: false,
  bot_token_set: true, app_token_set: true, bot_token_preview: 'xoxb-••••', app_token_preview: 'xapp-••••',
  owner_id: 'U000OWNER', command: 'kirocrew', allowed_enterprise_ids: [],
  reactions_enabled: true, show_thinking: true,
  agent_sessions: false, code_channels: true,
  code_channels_capability: { available: true, reason: '', scope: 'code_channels:manage' },
  agent_sessions_capability: { available: true, reason: '', scope: 'assistant:write' },
  code_channel_defaults: { folder: 'bbbbbbbbbbbb', agent: null, model: null, reasoning_effort: null, approval_mode: scene.approval },
  code_channel_effective: {
    folder: { id: 'bbbbbbbbbbbb', path: 'Dev/Slack Code', project_dir: '/repo/app', default_agent: 'builder' },
    agent: 'builder', agent_source: 'folder', model: null, reasoning_effort: null,
    project_dir: '/repo/app', project_source: 'folder', approval_mode: scene.approval ?? 'ask', slack_agent: 'kirocrew',
  },
  code_channel_warnings: [],
})

const NO_CHANGE = { added_scopes: [], removed_scopes: [], added_events: [], removed_events: [], added_features: [], removed_features: [] }
const manifestStatus = () => ({
  features: ['code_channels'], required_scopes: [], required_events: [], required_features: [],
  feature_changes: { agent_sessions: NO_CHANGE, code_channels: NO_CHANGE },
  install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: 'A0123ABCD', team_id: 'T1', reason: '' },
  app_manifest_url: null,
})

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 1100 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})

  const fixtures = {
    '/api/slack/config': () => slackConfig(),
    '/api/slack/manifest': () => ({ alias: 'kirocrew', manifest: 'display_information: {}\n', create_url: 'https://example.invalid/new' }),
    '/api/slack/manifest/status': () => manifestStatus(),
    '/api/chat/folders': () => [
      { id: 'aaaaaaaaaaaa', name: 'Dev', order: 0, project_dir: '/repo/app' },
      { id: 'bbbbbbbbbbbb', name: 'Slack Code', order: 1, parent_id: 'aaaaaaaaaaaa', default_agent: 'builder' },
    ],
    '/api/agents/installed': () => [{ name: 'builder' }, { name: 'reviewer' }],
    '/api/governance/channels': () => ({ slack: true, discord: true, telegram: true, webex: true, wecom: true, teams: true, weixin: true }),
    '/api/status': () => ({ sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' }),
    '/api/notifications': () => ({ notifications: [], unread: 0 }),
    '/api/auth/me': () => ({ user: 'owner', app: '' }),
    // A live, non-empty list: an empty one reads as degraded and the card shows
    // its "could not load the model list" notice, which is not this change.
    '/api/models': () => [{ model_name: 'auto', description: 'Auto' }, { model_name: 'claude-opus-5.5', description: '' }],
    '/api/themes': () => ({ themes: [], installed: [] }),
    '/api/theme/boot': () => ({ mode: scene.theme, theme: '' }),
    '/api/dashboard/branding': () => ({ bot_name: 'Kiro', avatar: '' }),
    '/api/recent-projects': () => ({ dirs: [] }),
    '/api/chat/slots': () => [],
    '/api/instances': () => ({ instances: [], active: '' }),
  }
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/kiro-prerequisite')) {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { status: 'idle', message: '' },
      })
    }
    if (fixtures[path]) return json(route, fixtures[path]())
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load() {
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-privacy-notice-v1', '1')
    }, scene.theme)
    await page.goto(`${BASE}/settings?tab=channels&channel=slack`, { waitUntil: 'domcontentloaded' })
    await page.getByTestId('slack-code-defaults').waitFor({ timeout: 15000 })
    await page.waitForTimeout(800)
  }

  const failures = []
  function check(cond, what) {
    if (!cond) failures.push(what)
    return cond
  }

  /** The one write path: a frame is written only when every check so far holds. */
  async function shot(name) {
    if (failures.length) throw new Error(`refusing to write ${name}: ${failures.join('; ')}`)
    const card = page.getByTestId('slack-code-defaults')
    await card.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await card.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const preview = () => page.getByTestId('slack-code-preview')
  const warning = () => page.getByTestId('slack-code-approval-warning')

  for (const theme of ['dark', 'light']) {
    scene.theme = theme
    scene.approval = null
    await load()
    check((await preview().innerText()).includes('Approvals'), `${theme}: preview has an Approvals row`)
    check((await preview().innerText()).includes('Normal'), `${theme}: default reads Normal`)
    check((await warning().count()) === 0, `${theme}: no warning on Normal`)
    check((await page.getByTestId('slack-code-defaults').getByText('Could not load').count()) === 0, `${theme}: no load-failure notice in frame`)
    await shot(`${theme}-1-default-normal`)

    await page.getByTestId('slack-code-defaults').getByLabel('Approval mode').click()
    await page.getByRole('option', { name: 'Trust', exact: true }).click()
    await warning().waitFor({ timeout: 5000 })
    const text = await warning().innerText()
    check(text.includes('anyone allowed to talk to the bot'), `${theme}: warning names who drives the agent`)
    check(text.includes('inherit'), `${theme}: warning says spawned sessions inherit`)
    const p = await preview().innerText()
    check(p.includes('Trust') && p.includes('Set here'), `${theme}: preview reads Trust · Set here`)
    await shot(`${theme}-2-trust-picked-warning`)
  }

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
