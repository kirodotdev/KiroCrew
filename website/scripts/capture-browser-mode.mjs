/**
 * Recording harness for the revamped Browser settings panel and the chat
 * composer's "+" drop-up menu.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. Nothing
 * in the panel is animated, so this captures still PNGs rather than video.
 *
 * ## The one moving part
 *
 * The panel renders entirely from GET /api/browser/config, so the different
 * scenes (disabled / enabled / firefox engine / attach mode) are produced by
 * mutating a single module-level fixture between navigations and reloading. The
 * SPA's own react-query refetch on reload picks up the new shape — the app code
 * on the config path is unmodified.
 *
 * The composer shot is evidence of a REMOVAL: the "+" menu used to carry a
 * "Let the agent use the browser" toggle at its foot, and that row is now gone.
 * The shot shows the menu with only its upload / command / file / skill rows.
 *
 * Usage: node scripts/capture-browser-mode.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/browser-mode'
const PROJECT = '/home/user/workspace/KiroCrew'
const CHAT_SLOT = 'chat-browser-mode'

mkdirSync(OUT, { recursive: true })

// The single mutable fixture the panel reads. Reassigned before each reload to
// drive the panel into a different scene; every field carries an explicit value
// so no scene depends on a component-side default.
const BASE_CONFIG = {
  enabled: false,
  engine: 'chromium',
  engines: ['chromium', 'firefox', 'webkit'],
  extension_mode: false,
  token: false,
  installed: true,
}
let browserConfig = { ...BASE_CONFIG }
const scene = (over) => { browserConfig = { ...BASE_CONFIG, ...over } }

// The PUT (save) echo. Default is a plain success; a scene can point it at a
// failed-install payload to render the failure advisory + manual-command block.
let putResponse = { ok: true, enabled: true, engine: 'chromium' }

// Chat-composer fixtures: one idle slot with an empty transcript, matching the
// shape ChatPage expects from /api/chat/slots and /api/chat/slots/<key>.
const chatSlots = [{
  key: CHAT_SLOT,
  title: 'Browser mode',
  running: false,
  last_message: '',
  messages: 0,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]
const chatDetail = {
  running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [],
}

const { srv, base } = await serveDist()

// No WebGL/canvas on these surfaces, so the swiftshader flags the animated
// harnesses need are omitted here.
const browser = await chromium.launch()

const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 2,
})

const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

// Chat page opens a gateway websocket on boot; accept and ignore it so the
// composer mounts without a connection-error banner.
await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const req = route.request()
  const path = new URL(req.url()).pathname

  // Panel under test. GET drives every scene; PUT / restart are the save path
  // the toggles fire — answered so a click never hangs, though the fixtures are
  // static so the reload (not the PUT echo) is what changes the rendered scene.
  if (path === '/api/browser/config') {
    if (req.method() === 'PUT') return json(route, putResponse)
    return json(route, browserConfig)
  }
  if (path === '/api/sessions/restart') return json(route, { ok: true })

  // Chat-composer boot calls.
  if (path === '/api/chat/slots') return json(route, chatSlots)
  if (path.startsWith('/api/chat/slots/')) return json(route, chatDetail)

  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(slot => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-active-slot-chat', slot)
}, CHAT_SLOT)

// ── Browser settings panel ─────────────────────────────────────────────────

const heading = () => page.getByRole('heading', { name: 'Browser Mode' })

/** Wait for the panel to settle after a (re)navigation, then screenshot. */
const shoot = async (name) => {
  await heading().waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}` })
}

/** Point the fixture at a new scene and reload the panel onto it. */
const reloadScene = async (over) => {
  scene(over)
  await page.reload({ waitUntil: 'domcontentloaded' })
}

// First scene: main toggle off, nothing else rendered.
scene({ enabled: false })
await page.goto(`${base}/settings?tab=browser`, { waitUntil: 'domcontentloaded' })
await shoot('browser-mode-disabled-dark.png')

// Enabled + headless: engine picker (Chromium/Firefox/WebKit) + attach toggle off.
await reloadScene({ enabled: true, extension_mode: false, engine: 'chromium' })
await shoot('browser-mode-enabled-dark.png')

// Light-theme variant of the enabled scene — flip the theme in place, shoot,
// flip back so the following dark shots are unaffected.
await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/browser-mode-enabled-light.png` })
await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
await page.waitForTimeout(300)

// Firefox engine: surfaces the "Playwright's own build" honesty note.
await reloadScene({ enabled: true, extension_mode: false, engine: 'firefox' })
await shoot('browser-mode-firefox-dark.png')

// Attach mode: Chromium-family store links + the connection-token input.
await reloadScene({ enabled: true, extension_mode: true, token: true })
await shoot('browser-mode-attach-dark.png')

// Provisioning advisory: enabled but the browser is not on disk yet
// (installed:false). The note renders as a MUTED info advisory, never a red
// error — this is the "never error on enable" surface. Written to its own dir so
// the browser-install PR embeds it directly.
mkdirSync(`${OUT}/../browser-install`, { recursive: true })
await reloadScene({ enabled: true, extension_mode: false, engine: 'chromium', installed: false })
await heading().waitFor({ timeout: 20000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/../browser-install/browser-panel.png` })

// Attempted-and-failed download: the save returns an install-failure carrying the
// sanitized cause + the copy-pasteable manual command. Start from a disabled scene,
// point the PUT echo at the failure payload, then flip the enable toggle so the
// save fires and the failure advisory + <code> block render.
putResponse = {
  ok: true,
  enabled: true,
  engine: 'chromium',
  install: {
    ok: false,
    step: 'browser',
    engine: 'chromium',
    reason: 'the npm registry rejected the request (E401 — auth/token invalid)',
    manual_command:
      'npm install -g @playwright/mcp@latest --registry=https://registry.npmjs.org/',
    detail:
      'Browser Mode is on, but the browser download did not finish. It normally '
      + 'downloads automatically on the first browse; if it keeps failing, install '
      + 'it by hand with the command below.  (Reason: the npm registry rejected the '
      + 'request (E401 — auth/token invalid).)',
  },
}
await reloadScene({ enabled: false, extension_mode: false, engine: 'chromium', installed: false })
await heading().waitFor({ timeout: 20000 })
await page.getByText('Enable Browser Mode').click()
// Wait for the manual-command <code> block from the failure payload to render.
await page.getByText('npm install -g @playwright/mcp', { exact: false }).waitFor({ timeout: 20000 })
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/../browser-install/browser-install-failed.png` })
putResponse = { ok: true, enabled: true, engine: 'chromium' }  // reset for later scenes

// ── Chat composer "+" menu (no browse toggle) ──────────────────────────────

let composerShot = null
try {
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  const composer = page.getByLabel('Message input')
  await composer.waitFor({ timeout: 20000 })
  // Open the drop-up menu; its rows are portaled to <body>.
  await page.getByRole('button', { name: 'Add files & options' }).click()
  await page.getByText('Upload file', { exact: true }).waitFor({ timeout: 5000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/chat-composer-menu-dark.png` })
  composerShot = 'chat-composer-menu-dark.png'
} catch (err) {
  // Menu open was flaky — fall back to the whole composer area as evidence the
  // toggle is gone from the bar itself.
  errors.push(`COMPOSER-MENU: ${err.message}`)
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/chat-composer-dark.png` })
  composerShot = 'chat-composer-dark.png'
}

await context.close()
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, composerShot, errors }, null, 2))
if (errors.length) process.exitCode = 1
