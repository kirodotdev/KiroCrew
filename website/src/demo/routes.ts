/**
 * The demo's answers to gateway API calls. Scripted routes cover the first-run
 * path a usability test walks (setup gate, import, privacy, the Customize tour,
 * a chat). Everything else gets a shape-safe empty answer, so a surface off that
 * path renders empty rather than error-bounding the shell.
 */
import { loadState, nextSeq, updateState, type DemoSlot, type SetupPhase } from './state'
import { streamReply } from './reply'
import { pushFrame } from './fakeSocket'

export interface DemoRequest {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
}

export interface DemoResponse {
  status: number
  body: unknown
  headers?: Record<string, string>
  delayMs?: number
  /** Answered by the generic fallback, not a scripted route. */
  miss?: boolean
}

const ok = (body: unknown, delayMs?: number): DemoResponse => ({ status: 200, body, delayMs })
const asRecord = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? v as Record<string, unknown> : {})

// ── Setup gate ───────────────────────────────────────────────────────────────

function prerequisiteStatus(phase: SetupPhase) {
  const installed = phase !== 'not_installed'
  const ready = phase === 'ready'
  return {
    platform: 'macOS',
    installed,
    authenticated: ready,
    ready,
    initial_setup_complete: ready,
    repair_required: false,
    docs_url: 'https://kiro.dev/docs/cli/installation/',
    login_command: 'kiro-cli login',
    sso_login_command: 'kiro-cli login --use-device-flow',
    bundled_cli: false,
    setup_allowed: true,
    can_auto_install: false,
    can_login: false,
    acp_supported: true,
    sandbox_backend_available: true,
    sandbox_blocked_backends: [],
    missing_agent_specs: [],
    rejected_agent_specs: [],
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  }
}

/** "Check again" is the tester's stand-in for running the shown command: each
 *  explicit check moves the gate one step closer to ready. */
function kiroPrerequisite(req: DemoRequest): DemoResponse {
  if (req.query.get('refresh') === 'explicit') {
    updateState((s) => {
      s.setup = s.setup === 'not_installed' ? 'signed_out' : 'ready'
    })
    return ok(prerequisiteStatus(loadState().setup), 900)
  }
  return ok(prerequisiteStatus(loadState().setup))
}

/** The setup screen's "Use other coding agents" list: the harnesses a stock
 *  build can select, none installed on the tester's (imaginary) host. The
 *  commands are the gateway's own (agent_sdk/backends_launch.py). */
const OTHER_AGENTS: [id: string, install: string][] = [
  ['claude', 'npm i -g @agentclientprotocol/claude-agent-acp'],
  ['codex', 'npm i -g @agentclientprotocol/codex-acp'],
  ['copilot', 'npm i -g @github/copilot@1.0.100'],
  ['opencode', 'npm i -g opencode-ai@1'],
  ['goose', 'curl -fsSL https://raw.githubusercontent.com/block/goose/main/download_cli.sh | bash'],
  ['deepseek', 'npm i -g @deepseek-ai/dsh@0.2.0-rc.2'],
  ['strands', 'npm i -g @strands-agents/cli@0.2.3'],
]

function acpBackends(): DemoResponse {
  return ok({
    backends: OTHER_AGENTS.map(([id, install]) => ({
      id,
      policy_id: id,
      selectable: true,
      independent_setup: true,
      installed: 'missing',
      missing_components: [id],
      install_command: install,
      restart_required: false,
    })),
  })
}

// ── First-run chapters ───────────────────────────────────────────────────────

const IMPORT_SCAN = {
  merge_only: true,
  sources: [
    // Source and category ids are the gateway's own (onboarding_sources/,
    // onboarding_scan.CATEGORY_IDS); the importer drops ids it does not know.
    {
      id: 'claude_code',
      name: 'Claude Code',
      detected: true,
      detail: '~/.claude',
      categories: [
        { id: 'instructions', label: 'Instructions', count: 1 },
        { id: 'memories', label: 'Memories', count: 4 },
        { id: 'mcp_servers', label: 'MCP servers', count: 2 },
        { id: 'skills', label: 'Skills', count: 3 },
      ],
    },
    {
      id: 'codex',
      name: 'Codex',
      detected: true,
      detail: '~/.codex',
      categories: [
        { id: 'instructions', label: 'Instructions', count: 1 },
        { id: 'mcp_servers', label: 'MCP servers', count: 1 },
        { id: 'settings', label: 'Settings', count: 2 },
      ],
    },
  ],
  skipped: [],
}

function importApply(req: DemoRequest): DemoResponse {
  const sources = asRecord(req.body).sources
  const picked = Array.isArray(sources) ? sources.length : 0
  return ok({
    ok: true,
    conflict_strategy: 'skip',
    summary: { imported: picked * 3, deduplicated: 0, skipped: 0, conflicts: 0, resolvable_conflicts: 0 },
  }, 1200)
}

function themeWrite(req: DemoRequest): DemoResponse {
  const body = asRecord(req.body)
  updateState((s) => {
    for (const k of Object.keys(s.theme) as (keyof typeof s.theme)[]) {
      if (k in body) (s.theme as Record<string, unknown>)[k] = body[k]
    }
  })
  return ok({ ok: true, ...loadState().theme })
}

// ── Config ───────────────────────────────────────────────────────────────────

/** `PATCH /api/config/kirocrew` carries one dotted `path` and its `value`. */
function configPatch(req: DemoRequest): DemoResponse {
  const { path, value } = asRecord(req.body)
  if (typeof path === 'string' && path.includes('.')) {
    const [section, ...rest] = path.split('.')
    updateState((s) => {
      s.config[section] = { ...(s.config[section] ?? {}), [rest.join('.')]: value }
    })
  }
  return ok({ ok: true })
}

// ── Sessions ─────────────────────────────────────────────────────────────────

function slotRow(slot: DemoSlot) {
  return {
    key: slot.key,
    title: slot.title,
    agent: slot.agent,
    running: false,
    messages: slot.messages.length,
    tags: [],
    last_ts: slot.last_ts,
    created: slot.created,
  }
}

function newSlot(body: Record<string, unknown>): DemoSlot {
  const now = new Date().toISOString()
  const key = typeof body.name === 'string' && body.name ? body.name : `chat-${Date.now().toString(36)}`
  const slot: DemoSlot = {
    key,
    title: typeof body.title === 'string' ? body.title : '',
    agent: typeof body.agent === 'string' && body.agent ? body.agent : 'kirocrew',
    created: now,
    last_ts: now,
    messages: [],
  }
  updateState((s) => { s.slots.unshift(slot) })
  return slot
}

function findSlot(key: string): DemoSlot | undefined {
  return loadState().slots.find((s) => s.key === key)
}

function slotDetail(key: string): DemoResponse {
  const slot = findSlot(key)
  const messages = slot?.messages ?? []
  return ok({ key, title: slot?.title ?? '', running: false, stopping: false, messages, queue: [], total: messages.length, has_more: false, next_before: 0 })
}

function sendChat(req: DemoRequest): DemoResponse {
  const body = asRecord(req.body)
  const message = typeof body.message === 'string' ? body.message : ''
  if (!message.trim()) return { status: 400, body: { error: 'empty message' } }
  const key = typeof body.slot === 'string' && body.slot ? body.slot : newSlot({}).key
  if (!findSlot(key)) newSlot({ name: key })
  const ts = new Date().toISOString()
  const mid = `demo-u-${nextSeq()}`
  let titled = ''
  updateState((s) => {
    const slot = s.slots.find((x) => x.key === key)
    if (!slot) return
    slot.messages.push({ role: 'user', content: message, cls: 'msg msg-u', ts, meta: { mid } })
    slot.last_ts = ts
    if (!slot.title) titled = slot.title = message.slice(0, 48)
  })
  // The gateway titles a session from its first message and pushes the change.
  if (titled) setTimeout(() => pushFrame('slot_title', { key, title: titled }), 600)
  streamReply(key)
  return ok({ ok: true, slot: key })
}

// ── Router ───────────────────────────────────────────────────────────────────

type Handler = (req: DemoRequest, match: RegExpMatchArray) => DemoResponse

const ROUTES: [string, RegExp, Handler][] = [
  ['GET', /^\/api\/kiro-prerequisite$/, kiroPrerequisite],
  ['GET', /^\/api\/acp-backends$/, acpBackends],
  ['GET', /^\/api\/theme\/boot$/, () => ok(loadState().theme)],
  ['PUT', /^\/api\/config\/theme$/, themeWrite],
  ['GET', /^\/api\/onboarding\/import\/scan$/, () => ok(IMPORT_SCAN, 1400)],
  ['POST', /^\/api\/onboarding\/import\/apply$/, importApply],
  ['PUT', /^\/api\/onboarding\/import\/state$/, () => ok({ ok: true })],
  ['GET', /^\/api\/config\/kirocrew$/, () => ok(loadState().config)],
  ['PATCH', /^\/api\/config\/kirocrew$/, configPatch],
  ['GET', /^\/api\/auth\/me$/, () => ok({ user: 'owner', app: '' })],
  ['GET', /^\/api\/status$/, () => ok({ sessions: loadState().slots.length, crons: 0, lessons: 0, uptime: 120, version: 'demo' })],
  ['GET', /^\/api\/notifications$/, () => ok({ notifications: [], unread: 0 })],
  ['GET', /^\/api\/models$/, () => ok({ models: [], default: 'auto' })],
  ['GET', /^\/api\/themes$/, () => ok({ themes: [], installed: [] })],
  ['GET', /^\/api\/dashboard\/branding$/, () => ok({ bot_name: 'Kiro', avatar: '' })],
  ['GET', /^\/api\/instances$/, () => ok({ instances: [], active: '' })],
  ['GET', /^\/api\/members$/, () => ok({ members: [] })],
  ['GET', /^\/api\/apps\/registry$/, () => ok({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' }, categoryOrder: [], editorialSections: [] })],
  ['GET', /^\/api\/apps\/registries$/, () => ok({ registries: [], pinned: [] })],
  ['GET', /^\/api\/recent-projects$/, () => ok({ dirs: [] })],
  ['POST', /^\/api\/chat\/nav\/resolve-links$/, () => ok({ summaries: [] })],
  ['GET', /^\/api\/chat\/slots$/, () => ok(loadState().slots.map(slotRow))],
  ['POST', /^\/api\/chat\/slots$/, (req) => ok(slotRow(newSlot(asRecord(req.body))))],
  ['GET', /^\/api\/chat\/slots\/([^/]+)$/, (_req, m) => slotDetail(decodeURIComponent(m[1]))],
  ['DELETE', /^\/api\/chat\/slots\/([^/]+)$/, (_req, m) => {
    const key = decodeURIComponent(m[1])
    updateState((s) => { s.slots = s.slots.filter((x) => x.key !== key) })
    return ok({ ok: true })
  }],
  ['POST', /^\/api\/chat$/, sendChat],
]

/**
 * The generic answer for a route the demo does not script, taken from the
 * screenshot harnesses' boot fixtures (`scripts/lib/boot-api.mjs`): object-shaped
 * for config/status-like reads, an empty list for everything else, and `{ok}`
 * for writes so a mutation's success path runs.
 */
function fallback(req: DemoRequest): DemoResponse {
  if (req.method !== 'GET' && req.method !== 'HEAD') return { status: 200, body: { ok: true }, miss: true }
  const objectish = /(config|tips|voice|autonudge|branding|status|usage|summary|state|settings|profile|prefs|detail)/.test(req.path)
  return { status: 200, body: objectish ? {} : [], miss: true }
}

export function resolveRoute(req: DemoRequest): DemoResponse {
  for (const [method, pattern, handler] of ROUTES) {
    if (method !== req.method) continue
    const m = req.path.match(pattern)
    if (m) return handler(req, m)
  }
  return fallback(req)
}
