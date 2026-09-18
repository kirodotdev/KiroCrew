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

/**
 * `auth`, as GET /api/acp-backends now sends it: one object per harness, projected
 * from that harness's own `AgentAuthDeclaration` in `agent_sdk/host_auth.py`.
 *
 * The panel no longer names a harness. It renders `sign_in_remedy` VERBATIM when
 * `signs_in_separately` is true, and says nothing otherwise — so these strings are
 * copied from the declarations rather than paraphrased. A scene carrying reworded
 * prose would be a screenshot of a sentence the server never emits, which is the
 * one thing a fixture harness must not do once the wording is server-owned.
 *
 * Keyed by `policy_id`, because that is the stable declaration name; the Kiro row's
 * wire `id` is the empty string.
 */
const AUTH = {
  claude: {
    sign_in_remedy:
      'Claude Code is a separate app you sign into yourself — run claude in ' +
      'your terminal and complete its sign-in. It is not checked here.',
    signs_in_separately: true,
  },
  codex: {
    sign_in_remedy:
      'Codex is a separate tool that signs in on its own — complete its ' +
      'sign-in, or name a model provider in ~/.codex/config.toml ' +
      '(CODEX_HOME moves that folder). Neither is checked here: the adapter ' +
      'reads them.',
    signs_in_separately: true,
  },
  // kiro-cli and KAS draw their entitlement from Crew's OWN identity store, so
  // there is no separate sign-in for an operator to finish and the panel prints
  // no caveat for them. The remedy string is still present on the row — what
  // suppresses the line is `signs_in_separately`, not an absent sentence, and a
  // scene that omitted it would be testing the wrong field.
  kiro: {
    sign_in_remedy: 'Run kiro-cli login in your terminal, then start a new chat.',
    signs_in_separately: false,
  },
  kas: {
    sign_in_remedy: 'Run kiro-cli login in your terminal, then start a new chat.',
    signs_in_separately: false,
  },
}


/**
 * The capability card, as GET /api/acp-backends now sends it per row.
 *
 * `CARD_LINES` is the server's own line ORDER (`backend_cards.USER_FACING_LINES`)
 * and `AVAILABLE` names, per harness, the lines the projection marks available —
 * copied from what the projection actually emits rather than invented, so a frame
 * documents a payload the server can genuinely produce. Keyed by `policy_id`, like
 * `AUTH` above.
 *
 * Written as "which lines are available" rather than as thirteen booleans per
 * harness because that is the form a reader can check against `backend_cards.py`
 * by eye.
 */
const CARD_LINES = [
  'crew_tools',
  'member_thread_tools',
  'member_saved_agent',
  'private_member_sessions',
  'side_chat_tools',
  'subagent_continuation',
  'mid_turn_steer',
  'manual_compact',
  'reasoning_effort',
  'model_switch',
  'markdown_agents',
]

const AVAILABLE = {
  kiro: [
    'crew_tools', 'member_saved_agent', 'private_member_sessions', 'side_chat_tools',
    'subagent_continuation', 'mid_turn_steer', 'manual_compact', 'reasoning_effort',
    'model_switch',
  ],
  kas: [
    'crew_tools', 'member_thread_tools', 'private_member_sessions', 'mid_turn_steer',
    'reasoning_effort', 'model_switch', 'markdown_agents',
  ],
  claude: [
    'crew_tools', 'member_thread_tools', 'private_member_sessions', 'manual_compact',
    'reasoning_effort', 'model_switch',
  ],
  codex: ['crew_tools', 'reasoning_effort', 'model_switch'],
  deepseek: ['crew_tools', 'reasoning_effort', 'model_switch'],
}

/**
 * The SECURITY notes each harness raises. The panel renders these OUTSIDE the
 * disclosure, beside the tool-approval line, so the frames show them with
 * nothing clicked.
 */
const SECURITY = {
  kiro: ['crew_sandbox_stands_down', 'pod_home_relocated'],
  kas: ['host_credential_to_child'],
}

/** The where-it-lives notes each harness raises. Rendered inside the disclosure. */
const NOTES = {
  kiro: ['crew_command_channel'],
  kas: ['crew_command_channel'],
  claude: ['own_credential_store', 'keeps_own_chat_record', 'harness_model_list'],
  codex: ['own_credential_store', 'keeps_own_chat_record', 'harness_model_list'],
  deepseek: ['own_credential_store', 'keeps_own_chat_record', 'harness_model_list'],
}

/** Each harness's routing mechanism, from `ACP_BACKEND_ROUTING`. */
const APPROVAL = {
  kiro: 'agent_spec',
  kas: 'agent_spec',
  claude: 'seeded_settings',
  codex: 'session_config',
  deepseek: 'unverified',
}

const card = policy_id => ({
  capabilities: CARD_LINES.map(id => ({
    id,
    available: (AVAILABLE[policy_id] || []).includes(id),
  })),
  security_notes: SECURITY[policy_id] || [],
  operator_notes: NOTES[policy_id] || [],
  tool_approval: APPROVAL[policy_id] || 'unverified',
  // deepseek is known and outside the selectable baseline: nothing establishes
  // that its tool calls reach the host gate, so the build never offers it.
  offered_by_build: policy_id !== 'deepseek',
})

/** One row of GET /api/acp-backends. */
const row = (id, policy_id, over = {}) => ({
  id,
  policy_id,
  selectable: true,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  ...(AUTH[policy_id] ? { auth: AUTH[policy_id] } : {}),
  ...card(policy_id),
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
 * Scene 5 — the two facts a harness can carry at once, on the one harness that has
 * both. Claude's tool gating has a caveat this frontend owns and translates, and
 * Claude signs in through its own credential file, which the SERVER states. They
 * are different facts with different remedies, so both lines print; an earlier
 * revision returned on the first one and the only row with two things to say said
 * one of them. Kiro and KAS sit beside it with no sign-in line at all, because
 * their entitlement comes from Crew's identity store.
 */
const SCENE_CLAUDE_BOTH = {
  schemaEnum: ['', 'kas', 'claude'],
  backends: [row('claude', 'claude'), row('kas', 'kas'), row('', 'kiro')],
}

/**
 * Scene 6 — Codex installed, and the one thing that verdict does not answer. The
 * adapter ships its own Codex binary, so `installed` really is the whole install
 * fact; a session can still die on its first turn for want of a credential. The
 * remedy names both branches and says outright that neither is checked here,
 * because the panel does not read those files — a `missing` verdict would disable
 * the switch for an operator who is authenticated by a path the check cannot see.
 *
 * Codex is also the proof that the panel holds no per-harness literal any more:
 * this frontend has no translated name for it, so its chip carries the wire
 * `policy_id`, and its whole caveat is the sentence the server sent.
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
 * Scene 7 — the two lines together on one row: the install line (what to run) above
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

/**
 * Scene 8 — an agent the BUILD never offers, which is a different state from one a
 * deployment denied. deepseek is in `ACP_BACKENDS_KNOWN` so a governance rule can
 * name it, and outside the selectable baseline because nothing establishes that its
 * tool calls reach the host gate. It gets a described row with no chip, and the
 * reason is its own tool-approval line rather than prose written for it.
 */
const SCENE_NOT_OFFERED = {
  schemaEnum: ['', 'kas', 'claude'],
  backends: [
    row('claude', 'claude'),
    row('deepseek', 'deepseek', { selectable: false }),
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

// Both Claude lines must be on screen before the shutter, so the frame cannot
// document a half-rendered row.
await reloadScene(SCENE_CLAUDE_BOTH)
await page.getByText("pre-approved in Claude's own settings", { exact: false }).waitFor({ timeout: 20000 })
await page.getByText('Claude Code is a separate app you sign into yourself', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-claude-both-lines.png')

await reloadScene(SCENE_CODEX)
await page.getByText('Codex is a separate tool that signs in on its own', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-codex-signin.png')

await reloadScene(SCENE_CODEX_MISSING)
await page.getByText('codex-acp', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-codex-missing.png')

// Scene 5's payload again, this time with the cards CLOSED and then OPEN. The
// summary count is what a reader compares across rows without opening anything,
// and the expanded frame is the only place the individual lines and the operator
// notes can be seen at all.
await reloadScene(SCENE_CLAUDE_BOTH)
await page.getByText('Kiro CLI supports 9 of 11 features', { exact: false }).waitFor({ timeout: 20000 })
await shoot('agent-backend-cards-collapsed.png')

for (const summary of await page.locator('summary').all()) await summary.click()
await page.getByText('Good to know').first().waitFor({ timeout: 20000 })
await shoot('agent-backend-cards-open.png')

await reloadScene(SCENE_NOT_OFFERED)
await page.getByText('This build does not offer this agent.').waitFor({ timeout: 20000 })
for (const summary of await page.locator('summary').all()) await summary.click()
await shoot('agent-backend-not-offered.png')

await browser.close()
srv.close()

if (errors.length) {
  console.error('console/page errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log(`wrote 10 frames to ${OUT}`)
