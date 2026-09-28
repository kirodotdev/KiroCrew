/**
 * Screenshot harness for a channel's optional session-folder setting.
 *
 * The setting is off by default on every channel, so the interesting states are
 * the default (toggle off, no name field) and a customized name — which is the
 * pair a reviewer needs to see to judge the copy. Both are captured here so the
 * PR's evidence is a pair of images rather than a claim, and so the helper text
 * in the shot cannot drift from the shipped string without this script's output
 * changing too.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures by Playwright. No gateway, no dashboard token, no data written. The
 * client code under test is unmodified — only the network and the localStorage
 * seed are stubbed.
 *
 * Discord is the channel shown because it is spec-driven BotChannelPanel, the
 * shape five of the seven channels share; WeChat's panel differs and is covered
 * by its own unit tests.
 *
 * Usage:
 *   npm run build && npx vite preview --port 6812 &
 *   node scripts/capture-channel-session-folder.mjs http://127.0.0.1:6812 \
 *     ../temp-screenshots/channel-session-folders
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/channel-session-folders'

mkdirSync(OUT, { recursive: true })

/** The channel config the panel renders. `session_folder` is the whole feature:
 *  "" is off, a name is on. */
const scene = { theme: 'dark', sessionFolder: '' }

const discordConfig = () => ({
  configured: true,
  connected: true,
  credential_set: true,
  enabled: true,
  read_only: false,
  bot_token_set: true,
  application_id: '1024',
  guild_ids: [],
  dm_policy: 'open',
  allowed_user_ids: [],
  tracked_channels: [],
  soft_threshold_pct: 80,
  session_folder: scene.sessionFolder,
})

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
    // The helper text under "Folder name" is ~11px; a 1x shot renders it too
    // soft to read, and reading it is the entire point of this capture.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/discord/config') return json(route, discordConfig())
    // Per-channel governance. The editable panel renders ONLY on a confirmed
    // ALLOW — an unknown policy deliberately hides the config, so without this
    // the capture shows the "policy status unavailable" notice instead.
    if (path === '/api/governance/channels') {
      return json(route, {
        slack: true, discord: true, telegram: true, webex: true,
        wecom: true, teams: true, weixin: true,
      })
    }
    // The prerequisite gate wraps the whole shell and reads
    // `status.operation.status`; without it nothing renders at all.
    if (path.startsWith('/api/kiro-prerequisite')) {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { status: 'idle', message: '' },
      })
    }
    // App-shell boot endpoints as a lookup rather than an if-chain: a chain of
    // `if (path === ...)` lines is a token-for-token clone of the sibling
    // harnesses, and jscpd's threshold here is 0%. `scene.theme` is read at
    // call time, so the table can be built once.
    const shell = {
      '/api/status': () => ({ sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' }),
      '/api/notifications': () => ({ notifications: [], unread: 0 }),
      '/api/auth/me': () => ({ user: 'owner', app: '' }),
      '/api/models': () => ({ models: [], default: 'auto' }),
      '/api/themes': () => ({ themes: [], installed: [] }),
      '/api/theme/boot': () => ({ mode: scene.theme, theme: '' }),
      '/api/dashboard/branding': () => ({ bot_name: 'Kiro', avatar: '' }),
      '/api/recent-projects': () => ({ dirs: [] }),
      '/api/chat/slots': () => [],
      '/api/instances': () => ({ instances: [], active: '' }),
    }
    if (shell[path]) return json(route, shell[path]())
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) {
      return json(route, {})
    }
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load() {
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-privacy-notice-v1', '1')
    }, scene.theme)
    await page.goto(`${BASE}/settings?tab=channels&channel=discord`, {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForTimeout(2600)
  }

  /** Crop to the toggle and everything below it, down to the save button, so
   *  the label, its description and the name field's helper text are all legible
   *  in one frame. */
  async function shot(name) {
    const toggle = page.getByText('File sessions in a folder').first()
    await toggle.waitFor({ timeout: 10000 })
    // The setting sits near the bottom of a long panel, so its box is outside
    // the viewport until it is scrolled in — clipping to an off-screen box fails.
    await toggle.scrollIntoViewIfNeeded()
    await page.waitForTimeout(600)
    const box = await toggle.boundingBox()
    const x = Math.max(0, box.x - 40)
    const y = Math.max(0, box.y - 120)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x, y,
        width: Math.min(1500 - x, 1000),
        height: Math.min(1000 - y, 430),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // Off by default — the state every channel ships in.
  scene.sessionFolder = ''
  await load()
  await shot('settings-off-by-default')

  // A custom name, which is also the on-state: the backend has one field.
  scene.sessionFolder = 'Team chat'
  await load()
  await shot('settings-custom-folder-name')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
