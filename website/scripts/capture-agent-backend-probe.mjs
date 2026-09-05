/**
 * Capture harness for Developer > Agent Backend, once it reads the machine probe.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. The panel
 * is static, so these are still PNGs rather than video.
 *
 * ## Why the scenes are fixtures rather than this machine's real state
 *
 * The panel composes TWO independent server facts, and one of the three verdicts
 * each fact can carry is unreachable on any single host: a public build never
 * reports `claude` as selectable, and `installed: "unknown"` only happens when the
 * probe itself raises. Shooting only the local truth would leave the two lines this
 * change exists for undocumented. Every scene below is therefore a payload the
 * server can genuinely emit; the FIRST one is this machine's real answer, recorded
 * from an unmocked probe call, and the others vary one field from it.
 *
 * Usage: node scripts/capture-agent-backend-probe.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-backend-probe'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const CLAUDE_INSTALL = 'npm i -g @agentclientprotocol/claude-agent-acp'

/** One row of GET /api/acp-backends. */
const row = (id, policy_id, over = {}) => ({
  id,
  policy_id,
  selectable: true,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  ...over,
})

/**
 * Scene 1 — what THIS host actually returns today. Claude Code IS selectable on a
 * public build (`acp/client.py` owns its whole spawn path and the adapter is a public
 * npm package), so the only thing standing between this operator and a Claude session
 * is the adapter itself, and the panel names it plus the command that installs it.
 */
const SCENE_LOCAL = {
  schemaEnum: ['', 'kas', 'claude'],
  backends: [
    row('claude', 'claude', {
      installed: 'missing',
      missing_components: ['claude-agent-acp'],
      install_command: CLAUDE_INSTALL,
    }),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

/**
 * Scene 2 — a managed deployment whose policy denies the harness. The row is GONE,
 * not greyed: a dimmed chip invites the reader to go find out how to enable it, and
 * there is nothing they can do from this machine. The footer sentence is what
 * explains the absence.
 */
const SCENE_DENIED = {
  schemaEnum: ['', 'kas'],
  backends: [
    row('claude', 'claude', {
      selectable: false,
      installed: 'missing',
      missing_components: ['claude-agent-acp'],
      install_command: CLAUDE_INSTALL,
    }),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

/**
 * Scene 3 — the check-failed line. `unknown` must leave the option ENABLED: the
 * probe could not answer, and disabling on that would send someone to install what
 * they may already have.
 */
const SCENE_UNKNOWN = {
  schemaEnum: ['', 'kas', 'claude'],
  backends: [
    row('claude', 'claude', { installed: 'unknown' }),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

/**
 * Scene 4 — the restart disclosure, and the one case where a POSITIVE install
 * verdict still disables the option. The adapter is on disk now, but this gateway
 * process already cached its absence, so a session started now would still fail.
 * Offering the control would be the "told you it was ready, then failed" trap.
 */
const SCENE_RESTART = {
  schemaEnum: ['', 'kas', 'claude'],
  backends: [
    row('claude', 'claude', { restart_required: true }),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

/**
 * Scene 5 — Codex installed, and the one thing that verdict does not answer. The
 * adapter ships its own Codex binary, so `installed` really is the whole install
 * fact; a session can still die on its first turn for want of a credential. The
 * caveat names both branches of the remedy and says outright that neither is
 * checked here, because the panel does not read those files — a `missing` verdict
 * would disable the switch for an operator who is authenticated by a path the
 * check cannot see.
 */
const SCENE_CODEX = {
  schemaEnum: ['', 'kas', 'claude', 'codex'],
  backends: [
    row('claude', 'claude'),
    row('codex', 'codex'),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

/**
 * Scene 6 — the two lines together on one row: the install line (what to run) above
 * the standing caveat (what running it still will not do). Only the first is a
 * measurement, and only the first disables the chip.
 */
const SCENE_CODEX_MISSING = {
  schemaEnum: ['', 'kas', 'claude', 'codex'],
  backends: [
    row('claude', 'claude'),
    row('codex', 'codex', {
      installed: 'missing',
      missing_components: ['codex-acp'],
      install_command: 'npm i -g @agentclientprotocol/codex-acp',
    }),
    row('kas', 'kas'),
    row('', 'kiro'),
  ],
}

let scene = SCENE_LOCAL

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname

  // The two facts the panel composes.
  if (path === '/api/acp-backends') return json(route, { backends: scene.backends })
  if (path === '/api/config/schema') {
    return json(route, {
      entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: scene.schemaEnum }],
    })
  }
  // Which option is pressed.
  if (path === '/api/config/kirocrew') return json(route, { agent: { acp_backend: '' } })

  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

const heading = () => page.getByText('Agent Backend', { exact: true }).first()

/** Wait for the card to settle after a (re)navigation, then screenshot it. */
const shoot = async (name) => {
  await heading().waitFor({ timeout: 20000 })
  // The status lines come from a second query; wait for one of its verdicts
  // rather than a bare timeout, so a scene can never be shot pre-hydration.
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/${name}` })
}

const reloadScene = async (next) => {
  scene = next
  await page.reload({ waitUntil: 'domcontentloaded' })
}

await page.goto(`${base}/developer?tab=agent-backend`, { waitUntil: 'domcontentloaded' })
await page.getByText(CLAUDE_INSTALL, { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-local.png')

await reloadScene(SCENE_DENIED)
await page
  .getByRole('button', { name: 'Claude Code' })
  .waitFor({ state: 'detached', timeout: 20000 })
await shoot('agent-backend-denied.png')

await reloadScene(SCENE_UNKNOWN)
await shoot('agent-backend-unknown.png')

await reloadScene(SCENE_RESTART)
await page.getByText('must restart', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-restart.png')

await reloadScene(SCENE_CODEX)
await page.getByText('Codex signs in on its own', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-codex-signin.png')

await reloadScene(SCENE_CODEX_MISSING)
await page.getByText('codex-acp', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-codex-missing.png')

await browser.close()
srv.close()

if (errors.length) {
  console.error('console/page errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log(`wrote 6 frames to ${OUT}`)
