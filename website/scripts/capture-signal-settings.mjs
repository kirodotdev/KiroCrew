/**
 * Screenshot harness for Settings → Messaging Channels → Signal, and for the
 * Signal row on Developer → System → Services.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server and answers every /api/** call from fixtures, so no gateway, no
 * signal-cli and no account are involved. Every number is from a fiction
 * range (+1 555 0100–0199).
 *
 * Scenes, each in the light AND the dark theme:
 *   - channels-list:             /settings/channels — the rail with the Signal
 *                                row and its live status line.
 *   - signal-panel:              the whole panel for a working dedicated-bot
 *                                setup (account absent from its own list, so
 *                                no Note-to-Self hint).
 *   - signal-needs-setup:        signal-cli missing, enabled, empty allow-list.
 *   - signal-connect-failed:     configured but signal-cli's start failed
 *                                (`connect_error` set).
 *   - signal-not-running:        configured and saved, not running, no error.
 *   - signal-account-invalid:    a number without its + typed into Account.
 *   - signal-no-account:         enabled with no account.
 *   - signal-note-to-self-hint:  disabled with an empty allow-list.
 *   - signal-saved:              the confirmation after Save.
 *   - signal-read-only:          the managed_locally notice (remote session).
 *   - signal-session-folder:     the folder toggle switched on, with its field.
 *   - signal-operator-warning:   a bot account with two allowed numbers, so
 *                                nobody is the operator.
 *   - services-signal-connected / services-signal-error:
 *                                /developer?tab=system&plane=services.
 *
 * Each scene names the text that proves its state rendered; a scene whose
 * proof never appears fails the run instead of saving the wrong picture.
 *
 * Usage: [SIGNAL_SCENES=<regex>] node scripts/capture-signal-settings.mjs [outDir]
 * (build first: npm run build). The default outDir is gitignored.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/signal-settings')
mkdirSync(OUT, { recursive: true })

/** `SIGNAL_SCENES=<regex>` captures only the matching scenes. */
const ONLY = process.env.SIGNAL_SCENES ? new RegExp(process.env.SIGNAL_SCENES) : null

const PROJECT = '/home/user/project'
const BOT = '+15550109876'
const OWNER = '+15550101234'
const FRIEND = '+15550101235'
const CONNECT_ERROR = `User ${BOT} is not registered.`

/** Every channel permitted by governance, so each detail pane is editable. */
const GOVERNANCE = Object.fromEntries(
  ['slack', 'discord', 'telegram', 'webex', 'wecom', 'feishu', 'teams', 'weixin', 'imessage', 'signal', 'whatsapp']
    .map(key => [key, true]),
)

/** The default detail pane (Slack) needs its full shape, or it error-bounds. */
const SLACK_UNSET = {
  connected: false, connect_error: '', configured: false, read_only: false,
  bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
  owner_id: '', command: '/kiro', allowed_enterprise_ids: [], reactions_enabled: true,
  show_thinking: false, session_folder: '',
}

/** Developer → System → Services reads `/api/system` for its process rows. */
const SYSTEM = {
  hostname: 'host', os: 'linux', arch: 'x86_64', cpu_count: 8,
  load_1m: 0, load_5m: 0, load_15m: 0,
  ip: '127.0.0.1', net_rx_mb: 0, net_tx_mb: 0, net_rx_kbs: 0, net_tx_kbs: 0,
  python: '3.12', pid: 4242, cwd: PROJECT,
  proc_mem_mb: 210, proc_cpu_pct: 1, child_processes: 0, thread_count: 12,
  mcp_total: 0,
}

/** The task-capacity card on the same page iterates `lanes`; an empty queue. */
const TASKS_SUMMARY = {
  generated_at: 1, available: true,
  depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
  oldest_wait_secs: 0, lanes: {}, degrade_reason: null, adaptive: null, slots: [], waiting: [],
  recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] }, stalled: {}, counts: {}, stall_after_secs: 600,
}

const SIGNAL_LIVE = {
  connected: true, connect_error: '', configured: true, read_only: false, cli_found: true,
  enabled: true, account: BOT, allowed_numbers: [OWNER], session_folder: '', operator_mode: 'sole_entry',
}
const SIGNAL_NEEDS_SETUP = {
  ...SIGNAL_LIVE, connected: false, configured: false, cli_found: false, allowed_numbers: [],
}
const SIGNAL_IDLE = { ...SIGNAL_LIVE, connected: false }

const SAVE_BUTTON = 'Save Signal settings'

/**
 * One scene: a path, the Signal payload it is served, the text that proves
 * the page mounted (`ready`), an optional interaction (`act`), and the text
 * that proves the intended STATE is on screen afterwards (`proof`).
 */
const SCENES = [
  { name: 'channels-list', path: '/settings/channels', signal: SIGNAL_LIVE, ready: 'Messaging Channels', height: 1100 },
  { name: 'signal-panel', path: '/settings/channels/signal', signal: SIGNAL_LIVE, ready: SAVE_BUTTON, height: 1700,
    absent: /to chat in Note to Self/ },
  { name: 'signal-needs-setup', path: '/settings/channels/signal', signal: SIGNAL_NEEDS_SETUP, ready: SAVE_BUTTON, height: 1700,
    proof: /it will answer nobody/ },
  { name: 'signal-connect-failed', path: '/settings/channels/signal', signal: { ...SIGNAL_IDLE, connect_error: CONNECT_ERROR },
    ready: SAVE_BUTTON, height: 1700, proof: /is not registered/ },
  { name: 'signal-not-running', path: '/settings/channels/signal', signal: SIGNAL_IDLE, ready: SAVE_BUTTON, height: 1700,
    proof: /the channel is not running yet/ },
  { name: 'signal-account-invalid', path: '/settings/channels/signal', signal: SIGNAL_LIVE, ready: SAVE_BUTTON, height: 1700,
    act: async page => { await page.getByLabel('Account number').fill(BOT.slice(1)) },
    proof: /with a leading \+, for example/ },
  { name: 'signal-no-account', path: '/settings/channels/signal', signal: { ...SIGNAL_IDLE, account: '' }, ready: SAVE_BUTTON,
    height: 1700, proof: /no account is set/ },
  { name: 'signal-note-to-self-hint', path: '/settings/channels/signal', signal: { ...SIGNAL_IDLE, enabled: false, allowed_numbers: [] },
    ready: SAVE_BUTTON, height: 1700, proof: /to chat in Note to Self/ },
  { name: 'signal-saved', path: '/settings/channels/signal', signal: SIGNAL_LIVE, ready: SAVE_BUTTON, height: 1700,
    act: async page => { await page.getByRole('button', { name: SAVE_BUTTON }).click() },
    proof: /^\s*Saved\.\s*$/ },
  { name: 'signal-read-only', path: '/settings/channels/signal', signal: { ...SIGNAL_LIVE, read_only: true },
    ready: 'Allowed numbers', height: 1700, proof: /read-only from a remote session/ },
  { name: 'signal-session-folder', path: '/settings/channels/signal', signal: SIGNAL_LIVE, ready: SAVE_BUTTON, height: 1700,
    act: async page => {
      await page.getByLabel('File sessions in a folder').click()
      await page.getByPlaceholder('Signal', { exact: true }).fill('Signal')
    },
    proof: /^Folder name$/ },
  { name: 'signal-operator-warning', path: '/settings/channels/signal',
    signal: { ...SIGNAL_LIVE, allowed_numbers: [OWNER, FRIEND], operator_mode: 'none' },
    ready: SAVE_BUTTON, height: 1700, proof: /Nobody can approve tools or use \/stop/ },
  { name: 'services-signal-connected', path: '/developer?tab=system&plane=services', services: { connected: true, error: '' },
    ready: 'Signal', height: 1100, rowText: /^SignalConnected$/ },
  { name: 'services-signal-error', path: '/developer?tab=system&plane=services', services: { connected: false, error: CONNECT_ERROR },
    ready: CONNECT_ERROR, height: 1100, rowText: new RegExp(`^SignalNot connected${CONNECT_ERROR.replace(/[.+]/g, '\\$&')}$`) },
]

/** The `dashboard` status frame Services renders its channel rows from. */
function statusFrame(signal) {
  return {
    uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
    slack_connected: false,
    channels: { slack: { connected: false, error: '' }, signal },
  }
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const fixedApi = makeFixedApi(PROJECT)

for (const theme of ['light', 'dark']) {
  for (const scene of SCENES.filter(s => !ONLY || ONLY.test(s.name))) {
    const context = await browser.newContext({ viewport: { width: 1400, height: scene.height }, deviceScaleFactor: 2, colorScheme: theme })
    const page = await context.newPage()
    page.on('pageerror', err => console.log(`PAGEERROR [${scene.name}]:`, String(err).slice(0, 400)))
    let ws = null
    await page.routeWebSocket(/\/api\/ws/, socket => { ws = socket })
    await page.route('**/api/**', route => {
      const p = new URL(route.request().url()).pathname
      const method = route.request().method()
      switch (p) {
        case '/api/chat/slots': return json(route, [])
        case '/api/governance/channels': return json(route, GOVERNANCE)
        case '/api/signal/config':
          if (method === 'PUT') return json(route, { ok: true, restart_required: false, verify_warning: '' })
          return json(route, scene.signal ?? SIGNAL_LIVE)
        case '/api/slack/config': return json(route, SLACK_UNSET)
        case '/api/slack/manifest': return json(route, { alias: 'kiro', manifest: '{}', create_url: '' })
        case '/api/system': return json(route, SYSTEM)
        case '/api/tasks/summary': return json(route, TASKS_SUMMARY)
        case '/api/mcp-gateway/status': return json(route, { enabled: false, running: false, ping_ok: false, stub: [], stub_count: 0, supported: true })
        case '/api/mcp-gateway/metrics': return json(route, { running: false, backends: [] })
        default:
          // Every other channel reads as present but not set up.
          if (/^\/api\/[a-z]+\/config$/.test(p)) {
            return json(route, { connected: false, connect_error: '', configured: false, read_only: false, enabled: false, allowed_user_ids: [] })
          }
          return handleBootRoute(route, p, { project: PROJECT, theme, fixedApi })
      }
    })
    await page.addInitScript(mode => {
      localStorage.clear()
      for (const flag of ['mc-onboarded', 'mc-import-onboarded', 'mc-privacy-acked']) localStorage.setItem(flag, '1')
      localStorage.setItem('mc-theme', mode)
      localStorage.setItem('mc-lang', 'en')
    }, theme)
    await page.goto(`${base}${scene.path}`, { waitUntil: 'domcontentloaded' })

    if (scene.services) {
      // The channel rows come off the pushed `dashboard` status frame. The socket's
      // listener attaches after the page boots, so re-send until the row the
      // scene is about is on screen rather than sleeping a guessed interval.
      const row = page.getByText(scene.ready, { exact: true }).first()
      for (let tries = 0; tries < 40; tries++) {
        if (ws) ws.send(JSON.stringify({ type: 'dashboard', data: statusFrame(scene.services) }))
        try { await row.waitFor({ timeout: 500 }); break } catch { /* not yet */ }
      }
      await row.waitFor({ timeout: 5_000 })
      // The Signal row of Messaging channels, not a stray "Signal" elsewhere:
      // its label cell's parent holds exactly the label and the status cell.
      const cells = await page.getByText('Signal', { exact: true }).locator('xpath=..').allTextContents()
      if (!cells.some(t => scene.rowText.test(t.trim()))) {
        throw new Error(`${scene.name}: no Signal row matching ${scene.rowText} (saw ${JSON.stringify(cells)})`)
      }
    } else {
      await page.getByText(scene.ready, { exact: true }).first().waitFor({ timeout: 20_000 })
      // The rail's status line comes from its own query; wait for it on the
      // Signal row rather than on a timer.
      await page.getByRole('option', { name: /Signal/ })
        .filter({ hasText: /Connected|Needs setup|Not connected/ })
        .waitFor({ timeout: 20_000 })
    }
    if (scene.act) await scene.act(page)
    if (scene.proof) {
      try {
        await page.getByText(scene.proof).first().waitFor({ timeout: 10_000 })
      } catch (err) {
        await page.screenshot({ path: join(OUT, `FAILED-${scene.name}-${theme}.png`) })
        throw err
      }
    }
    if (scene.absent && await page.getByText(scene.absent).count()) {
      throw new Error(`${scene.name}: ${scene.absent} must not render`)
    }
    const file = join(OUT, `${scene.name}-${theme}.png`)
    // The settings pane scrolls inside its own container, so `fullPage` would not
    // reach the save row; each scene sets a viewport tall enough instead.
    await page.screenshot({ path: file })
    console.log(`captured ${file}`)
    await context.close()
  }
}

await browser.close()
srv.close()
