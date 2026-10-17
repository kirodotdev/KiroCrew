/**
 * Screenshot harness for Settings → Messaging Channels → Signal.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server and answers every /api/** call from fixtures, so no gateway, no
 * signal-cli and no account are involved.
 *
 * Scenes, each in the light AND the dark theme:
 *   - channels-list: /settings/channels — the channel rail with the Signal row
 *     and its live status line (the detail pane shows Slack, the default).
 *   - signal-panel:  /settings/channels/signal — the whole Signal panel:
 *     the two setups, account, allow-list and save row.
 *   - signal-needs-setup: the same panel with signal-cli missing and the
 *     channel enabled with an empty allow-list, so the notices render.
 *
 * Usage: node scripts/capture-signal-settings.mjs [outDir]
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

const PROJECT = '/home/user/project'
const BOT = '+15550109876'
const OWNER = '+15550101234'

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

const SIGNAL_LIVE = {
  connected: true, connect_error: '', configured: true, read_only: false, cli_found: true,
  enabled: true, account: BOT, allowed_numbers: [OWNER], session_folder: '',
}
const SIGNAL_NEEDS_SETUP = {
  ...SIGNAL_LIVE, connected: false, configured: false, cli_found: false, allowed_numbers: [],
}

/** One scene: a path, the Signal payload it is served, and what proves it rendered. */
const SCENES = [
  { name: 'channels-list', path: '/settings/channels', signal: SIGNAL_LIVE, ready: 'Messaging Channels', height: 1100 },
  { name: 'signal-panel', path: '/settings/channels/signal', signal: SIGNAL_LIVE, ready: 'Save Signal settings', height: 1700 },
  { name: 'signal-needs-setup', path: '/settings/channels/signal', signal: SIGNAL_NEEDS_SETUP, ready: 'Save Signal settings', height: 1700 },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const fixedApi = makeFixedApi(PROJECT)

for (const theme of ['light', 'dark']) {
  for (const scene of SCENES) {
    const context = await browser.newContext({ viewport: { width: 1400, height: scene.height }, deviceScaleFactor: 2, colorScheme: theme })
    const page = await context.newPage()
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.route('**/api/**', route => {
      const p = new URL(route.request().url()).pathname
      switch (p) {
        case '/api/chat/slots': return json(route, [])
        case '/api/governance/channels': return json(route, GOVERNANCE)
        case '/api/signal/config': return json(route, scene.signal)
        case '/api/slack/config': return json(route, SLACK_UNSET)
        case '/api/slack/manifest': return json(route, { alias: 'kiro', manifest: '{}', create_url: '' })
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
    await page.getByText(scene.ready, { exact: true }).first().waitFor({ timeout: 20_000 })
    // The rail's status line comes from its own query; wait for it on the
    // Signal row rather than on a timer.
    await page.getByRole('option', { name: /Signal/ })
      .filter({ hasText: /Connected|Needs setup|Not connected/ })
      .waitFor({ timeout: 20_000 })
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
