/**
 * Capture harness for the card's THIRD capability state: not measured.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. The card is
 * static, so these are PNGs rather than video.
 *
 * ## Why these payloads are not invented
 *
 * Every `capabilities` row below is copied verbatim from what
 * `backend_cards.card_payload()` emits for that harness on this revision, so a frame
 * documents the answer the server actually gives rather than a shape hand-written to
 * make a point. pi and goose are the two harnesses whose `/compact` cell is declared
 * unmeasured (`DECLARED_UNMEASURED`, from `ACP_BACKENDS_COMPACT`'s own "unclassified"
 * words), and kiro-cli sits beside them as the all-measured comparison — which is what
 * makes the three marks comparable in one read.
 *
 * Usage: node scripts/capture-agent-backend-unmeasured.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-backend-unmeasured'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** `card_payload("pi")` on this revision. */
const PI_CAPABILITIES = [
  { id: 'crew_tools', available: false, measured: true, unmeasured_reason: '' },
  { id: 'member_thread_tools', available: false, measured: true, unmeasured_reason: '' },
  { id: 'member_saved_agent', available: false, measured: true, unmeasured_reason: '' },
  { id: 'side_chat_tools', available: false, measured: true, unmeasured_reason: '' },
  { id: 'subagent_continuation', available: false, measured: true, unmeasured_reason: '' },
  { id: 'mid_turn_steer', available: false, measured: true, unmeasured_reason: '' },
  {
    id: 'manual_compact',
    available: false,
    measured: false,
    unmeasured_reason: 'no_driven_capture',
  },
  { id: 'reasoning_effort', available: false, measured: true, unmeasured_reason: '' },
  { id: 'model_switch', available: true, measured: true, unmeasured_reason: '' },
  { id: 'markdown_agents', available: false, measured: true, unmeasured_reason: '' },
]

/** `card_payload("goose")` on this revision: same unmeasured cell, more available. */
const GOOSE_CAPABILITIES = PI_CAPABILITIES.map(line =>
  line.id === 'crew_tools' ? { ...line, available: true } : line,
)

/** `card_payload("")` — kiro-cli, measured on every line. */
const KIRO_CAPABILITIES = PI_CAPABILITIES.map(line => ({
  ...line,
  available: !['member_thread_tools', 'markdown_agents'].includes(line.id),
  measured: true,
  unmeasured_reason: '',
}))

const ROWS = {
  pi: {
    id: 'pi',
    policy_id: 'pi',
    capabilities: PI_CAPABILITIES,
    security_notes: [],
    operator_notes: ['own_credential_store'],
    tool_approval: 'verified_gate_extension',
    mcp: { per_tool_deny: '', costs_whole_server: false, ineffective: [] },
    auth: {
      sign_in_remedy: 'pi signs in on its own — complete its sign-in in your terminal.',
      signs_in_separately: true,
    },
  },
  goose: {
    id: 'goose',
    policy_id: 'goose',
    capabilities: GOOSE_CAPABILITIES,
    security_notes: ['refuses_unclassified_tools'],
    operator_notes: ['own_credential_store'],
    tool_approval: 'verified_seeded_settings',
    mcp: {
      per_tool_deny: 'whole-server',
      costs_whole_server: true,
      ineffective: ['auto_approve', 'permission_mode', 'model_allowlist', 'hooks'],
    },
  },
  kiro: {
    id: '',
    policy_id: 'kiro',
    capabilities: KIRO_CAPABILITIES,
    security_notes: ['crew_sandbox_stands_down', 'pod_home_relocated'],
    operator_notes: [],
    tool_approval: 'agent_spec',
    mcp: { per_tool_deny: '', costs_whole_server: false, ineffective: [] },
  },
}

/** One row of GET /api/acp-backends. */
const row = name => ({
  selectable: true,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  offered_by_build: true,
  ...ROWS[name],
})

const SCENE = {
  schemaEnum: ['', 'pi', 'goose'],
  backends: [row('goose'), row('pi'), row('kiro')],
}

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 980 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => {
  if (m.type() === 'error') errors.push(m.text().slice(0, 200))
})

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/acp-backends') return json(route, { backends: SCENE.backends })
  if (path === '/api/config/schema') {
    return json(route, {
      entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: SCENE.schemaEnum }],
    })
  }
  if (path === '/api/config/kirocrew') return json(route, { agent: { acp_backend: '' } })
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

const heading = () => page.getByText('Agent Backend', { exact: true }).first()

const shoot = async name => {
  await heading().waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/${name}` })
}

/** Put one harness's detail on screen. Highlighting is not selecting. */
const highlight = async name => {
  await page.getByRole('tab', { name }).click()
}

await page.goto(`${base}/developer?tab=agent-backend`, { waitUntil: 'domcontentloaded' })

// Every string the frame is EVIDENCE for is waited on individually, so the shutter
// cannot fire on a half-rendered card or document an older draft of the copy.
await highlight('pi')
await page.getByText('The /compact command works', { exact: false }).waitFor({ timeout: 20000 })
await page
  .getByText('Nobody has tried this on a real session yet', { exact: false })
  .waitFor({ timeout: 20000 })
await page.getByText('plus 1 not checked', { exact: false }).waitFor({
  timeout: 20000,
})
await shoot('agent-backend-unmeasured-pi.png')

await highlight('goose')
await page
  .getByText('Nobody has tried this on a real session yet', { exact: false })
  .waitFor({ timeout: 20000 })
await shoot('agent-backend-unmeasured-goose.png')

// The comparison frame: a harness measured on every line, so the reader can see that
// the third mark is a third mark rather than a restyled cross.
await highlight('Kiro CLI')
await page.getByText('supports 8 of 10 features', { exact: false }).first().waitFor({
  timeout: 20000,
})
await shoot('agent-backend-unmeasured-kiro-comparison.png')

await browser.close()
srv.close()

if (errors.length) {
  console.error('console/page errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log(`wrote 3 frames to ${OUT}`)
