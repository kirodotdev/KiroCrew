import { api } from '../../api/client'
import modelTokensRaw from '../../model_tokens.json'
import { markModelsDegraded } from '../modelListHealth'
import { isPricedMultiplier } from '../modelList'
import type {
  ProviderAdapter,
  ProviderCapabilities,
  ProviderLabels,
  AgentBinding,
  NormalizedUsage,
  NormalizedProviderHook,
  NormalizedPlugin,
  ModelInfo,
  PermissionMode,
} from '../types'

const MODEL_TOKENS: Record<string, number> = Object.fromEntries(
  Object.entries(modelTokensRaw).filter(([k]) => !k.startsWith('_')) as [string, number][]
)
const DEFAULT_CONTEXT = 200_000

// Windows learned from GET /api/models, which enriches every row with the
// backend's centrally-resolved `context_window` (kiro `--list-models` cache >
// static registry > supplementary map > [1m] heuristic). That resolver knows
// every model kiro actually serves; the bundled static map above is a 21-entry
// snapshot that does NOT (claude-opus-5, gpt-5.6-sol, glm-5, kimi-k3, grok-4.3,
// …). Without this cache, `getContextWindow` fell through to DEFAULT_CONTEXT for
// those, so the composer's context meter read "0% of 200K" right after a model
// switch and only became correct once the first turn's live usage_update landed.
// Keyed by the same model id the picker sends and slot.model stores.
const LIVE_WINDOWS: Record<string, number> = {}

/** Record an authoritative per-model window so the window lookup no longer
 *  depends on the bundled snapshot. Non-positive values are ignored: a row that
 *  reports no window must not overwrite a previously-learned good one, and it
 *  must not record the caller's own default as if the backend had said it. */
function learnWindow(name: string, window: number): void {
  if (name && Number.isFinite(window) && window > 0) LIVE_WINDOWS[name] = window
}

/** Narrow view of GET /api/config/kirocrew — only the fields the composer needs
 *  to resolve what a new session will actually run on. */
interface KirocrewAgentConfig {
  agent?: { model?: string; reasoning_effort?: string }
}

// Persist the last SUCCESSFUL live /api/models list so a transient backend
// failure (a creds/token hiccup, or a cold `--list-models` spawn exceeding the
// gateway's timeout → 503) degrades to the real, last-known-good list instead
// of collapsing to auto-only. The cached ids came from the backend, so they are
// valid ACP model ids — unlike the canonical registry keys (fable-5-1m, …)
// which the ACP CLI rejects (-32603). A TTL bounds staleness: entitlements /
// available models can change while the backend is unreachable, so a very old
// cache could still offer a model the CLI no longer accepts (a residual, now
// time-boxed, -32603 window). Versioned key so a shape change invalidates.
const MODELS_CACHE_KEY = 'kc.acp.models.v1'
const MODELS_CACHE_TTL_MS = 24 * 60 * 60 * 1000 // 24h — bound stale-model exposure

interface CachedModels {
  ts: number
  models: ModelInfo[]
  /** Which ACP backend's namespace these rows belong to (`''` = kiro).
   *
   *  Load-bearing, not bookkeeping: model ids are per-backend namespaces, so a
   *  list cached under one backend is WRONG under another — and wrong in the
   *  most dangerous way, because a `gpt-5.6-sol` row reads as a real option and
   *  is only rejected at the wire. An unstamped entry (written by an older
   *  build) is treated as unusable rather than assumed to be kiro's. */
  backend?: string
}

/** Last backend a /api/models response identified itself as (`''` = kiro).
 *
 *  Needed because a bodyless failure (network down, gateway restarting) carries
 *  no backend, yet the failure path still wants to know whether a cached list is
 *  safe to serve. */
const LAST_BACKEND_KEY = 'kc.acp.backend.v1'

function rememberBackend(backend: string): void {
  try {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(LAST_BACKEND_KEY, backend)
  } catch {
    /* storage disabled — the cache guard just gets stricter, never looser */
  }
}

/** Whether the active backend serves the `auto` sentinel, as the gateway reported
 *  it (`ACP_BACKENDS_AUTO_MODEL`).
 *
 *  Remembered rather than recomputed because the answer is needed in exactly the
 *  state that carries no response: a cold start whose first fetch has not landed,
 *  or a bodyless network failure. Never mirrored as a list of backend ids here —
 *  the gateway owns the membership, so adding a backend to it needs no client
 *  change. */
const LAST_SERVES_AUTO_KEY = 'kc.acp.servesAuto.v1'

function rememberServesAuto(servesAuto: boolean): void {
  try {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(LAST_SERVES_AUTO_KEY, servesAuto ? '1' : '0')
  } catch {
    /* storage disabled — falls back to the fail-closed default below */
  }
}

/**
 * Whether the active backend serves `auto`, as last reported by the gateway.
 *
 * Absence resolves to TRUE, which is the kiro answer. That direction is required,
 * not preferred: `agent.acp_backend` defaults to kiro and kiro is the floor
 * (harness-parity H4), so a browser that has never seen a response must behave
 * exactly as it did before any adapter existed. Defaulting to `false` would strip
 * the Auto row from a fresh kiro browser during a cold start or a transient 503 —
 * changing the Kiro path to accommodate an added harness, which H1 forbids.
 *
 * The exposure that buys is one response wide. Every shape that identifies a
 * non-kiro backend carries `serves_auto`, including the degraded 503 — which the
 * gateway returns BEFORE any subprocess spawn, so it arrives promptly rather than
 * after a cold-start timeout. The answer is then sticky in localStorage, so only
 * the very first paint of a never-before-seen browser can show an Auto row on an
 * adapter that lacks it.
 */
export function servesAutoModel(): boolean {
  try {
    if (typeof localStorage === 'undefined') return true
    return localStorage.getItem(LAST_SERVES_AUTO_KEY) !== '0'
  } catch {
    return true
  }
}

/** The last identified backend, or null when we have never seen one.
 *
 *  null (not `''`) on absence: defaulting to kiro would let a fresh browser
 *  serve a kiro-stamped cache while a spec adapter is active. */
export function lastKnownBackend(): string | null {
  try {
    if (typeof localStorage === 'undefined') return null
    return localStorage.getItem(LAST_BACKEND_KEY)
  } catch {
    return null
  }
}

/** Read the last-good live model list from localStorage, or null if absent,
 *  expired (older than the TTL), or unusable. Fully guarded: SSR (no
 *  localStorage), disabled storage, quota, and corrupt JSON all degrade to null
 *  so the caller falls through to auto-only. */
function readCachedModels(expected: string | null): ModelInfo[] | null {
  try {
    if (typeof localStorage === 'undefined') return null
    // No identified backend yet, or an entry from a build that did not stamp
    // one: refuse. "Cannot tell" must not resolve to "serve it".
    if (expected === null) return null
    const raw = localStorage.getItem(MODELS_CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachedModels
    if (!parsed || typeof parsed.ts !== 'number' || !Array.isArray(parsed.models)) return null
    if (typeof parsed.backend !== 'string' || parsed.backend !== expected) return null
    const age = Date.now() - parsed.ts
    // Reject a stale cache AND a future timestamp: a cache written while the
    // clock was ahead yields a negative age after the clock is corrected, which
    // would otherwise slip past a `> TTL` check and be served indefinitely.
    if (age < 0 || age > MODELS_CACHE_TTL_MS) return null
    const clean = parsed.models.filter(
      (m): m is ModelInfo =>
        !!m && typeof m.name === 'string' && m.name.length > 0
    )
    return clean.length > 0 ? clean : null
  } catch {
    return null
  }
}

/** Seed `LIVE_WINDOWS` from the persisted list at module load so a cold start
 *  (first paint, before /api/models resolves) already resolves the real window
 *  for the slot's current model instead of falling back to DEFAULT_CONTEXT. The
 *  cached rows carry whatever `fetchAvailableModels` stored — the reported window
 *  when the backend gave one, else its own fallback, which is the value the
 *  lookup would have produced anyway. Either way the next live response corrects
 *  it. */
for (const m of readCachedModels(lastKnownBackend()) ?? []) learnWindow(m.name, m.contextWindow ?? 0)

/** Persist a live model list with a timestamp. Best-effort — storage errors
 *  (quota, disabled, SSR) are swallowed so caching never breaks the picker. */
function writeCachedModels(models: ModelInfo[], backend: string): void {
  try {
    if (typeof localStorage === 'undefined') return
    const payload: CachedModels = { ts: Date.now(), models, backend }
    localStorage.setItem(MODELS_CACHE_KEY, JSON.stringify(payload))
  } catch {
    /* quota exceeded / storage disabled — non-fatal */
  }
}

/** Raw daily-usage entry from /api/usage/kiro. */
interface RawDailyHistory {
  date: string
  sessions: number
  messages: number
  tool_calls: number
}

/** Raw provider-hook entry from /api/kiro-hooks. */
interface RawHookEntry {
  command: string
  matcher?: string
  source?: string
}

/** Raw plugin (agent/skill/mcp) entry from /api/capability/*. */
interface RawPlugin {
  name: string
  package?: string
  server_id?: string
  version?: string
}

/** Raw model entry from /api/models. */
interface RawModel {
  model_name: string
  description?: string
  /** Backend-resolved context window. Two spellings because the endpoint has two
   *  branches: the kiro path returns `kiro-cli --list-models` rows verbatim
   *  (`context_window_tokens`), the claude_code path enriches its merged rows via
   *  the central resolver (`context_window`). Both are authoritative and cover
   *  models the bundled static map has never heard of. Optional so a gateway
   *  predating either field still works (we fall back to the map). */
  context_window?: number
  context_window_tokens?: number
  /** Relative credit cost of a turn on this model, Auto = 1.0. Optional: a
   *  gateway/kiro-cli predating the field simply omits it, and we render no
   *  badge rather than inventing a price (see ModelInfo.rateMultiplier). */
  rate_multiplier?: number
}

/** The window a /api/models row reports, or 0 when it reports none. */
function rowWindow(m: RawModel): number {
  for (const v of [m.context_window, m.context_window_tokens]) {
    if (typeof v === 'number' && Number.isFinite(v) && v > 0) return v
  }
  return 0
}

/** The credit multiplier a /api/models row reports, or undefined when it
 *  reports none (or reports something that is not a price — see
 *  isPricedMultiplier). */
function rowMultiplier(m: RawModel): number | undefined {
  return isPricedMultiplier(m.rate_multiplier) ? m.rate_multiplier : undefined
}

/** Did the gateway refuse because the active ACP backend's model namespace is
 *  not knowable yet, rather than because the call transiently failed?
 *
 *  Reads the raw body STRUCTURALLY rather than testing `e instanceof ApiError`:
 *  class identity is not reliable here. It does not survive a partial module
 *  mock, and it breaks across bundle realms where two copies of the module each
 *  define their own class object. The `body` field is the contract.
 *
 *  Matched on the machine-readable `code`, never the message: the human text is
 *  translated into 12 locales and localized prose is not a protocol. */
function isBackendNamespaceUnavailable(e: unknown): boolean {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string') return false
  try {
    return JSON.parse(body)?.code === 'acp_backend_models_unavailable'
  } catch {
    return false
  }
}

/** The backend an ERROR body names, or null when it names none.
 *
 *  Same `body`-field contract as {@link isBackendNamespaceUnavailable}, and for
 *  the same reason: class identity does not survive a partial module mock or a
 *  second bundle realm. A gateway error that identifies its namespace lets the
 *  failure path judge the cache instead of guessing. */
function backendFromErrorBody(e: unknown): string | null {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string') return null
  try {
    const parsed = JSON.parse(body)
    return typeof parsed?.backend === 'string' ? parsed.backend : null
  } catch {
    return null
  }
}

/** Whether an ERROR body affirms that the active backend serves `auto`, or null
 *  when it says nothing about it.
 *
 *  The degraded 503 is the response that matters most here — it is the steady
 *  state of an adapter with no live session yet — so it carries `serves_auto`
 *  alongside `backend`. Tri-state on purpose: a body from an older gateway omits
 *  the field, and `false` would then be read as a denial rather than as silence,
 *  wrongly stripping a kiro-family operator's Auto row. */
function servesAutoFromErrorBody(e: unknown): boolean | null {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string') return null
  try {
    const parsed = JSON.parse(body)
    return typeof parsed?.serves_auto === 'boolean' ? parsed.serves_auto : null
  } catch {
    return null
  }
}

export class AcpAdapter implements ProviderAdapter {
  readonly id = 'acp' as const
  readonly displayName = 'ACP'

  readonly capabilities: ProviderCapabilities = {
    hooks: true,
    pluginRegistry: true,
    agentTemplates: true,
    usageBilling: true,
    warmPool: true,
    modelResolution: true,
    toolExecution: true,
    subagents: true,
    sandbox: true,
    contextWindow: true,
    modelSwitching: true,
    permissionModes: true,
    sessionResume: true,
    compaction: true,
    // The ACP CLI supports a reasoning-effort control (low|medium|high) on
    // effort-capable models. The per-model capability gate lives in the
    // dashboard (the dropdown only shows when the active session's model is
    // effort-capable).
    reasoningEffort: true,
  }

  readonly labels: ProviderLabels = {
    sessionProcess: 'ACP subprocess',
    agentTemplateField: 'Agent Template',
    processCountLabel: 'acp_cli',
    warmPoolDescription: 'Pre-spawn ACP CLI processes for instant session start.',
    configFile: 'kirocrew.json',
    pluginRegistryName: 'Packages',
    hooksSection: 'ACP Agent Hooks',
  }

  resolveAgentTemplate(agent: AgentBinding): string {
    return agent.kiro_agent || agent.name
  }

  async resolveModel(agentName: string): Promise<string> {
    // Ask the backend which model a new session on this agent would run on, so
    // the composer shows the real value before a session exists to report one.
    //
    // This deliberately does NOT re-derive the precedence client-side. The
    // chain is four tiers deep (the KiroCrew agent's own model, the bound kiro
    // agent's pin, the global agent.model default, the installed agent file)
    // and a second copy of it here would drift from the backend's: a fresh slot
    // would display the kiro agent file's model while the turn actually ran on
    // the configured default, and the mismatch would only self-correct once the
    // first turn backfilled slot.model from the live session.
    //
    // `agentName` is a KiroCrew agent name (a "crew"), not a kiro agent
    // template — the per-agent default is stored per crew, and several crews can
    // share one template.
    try {
      const d = await api.agentResolvedModel(agentName)
      return d?.model || ''
    } catch {
      return ''
    }
  }

  /** KiroCrew's configured default model (Settings → Chat → Default Model).
   *  '' when unset or "auto" — both mean "no explicit default", so callers fall
   *  through to the agent-file model exactly as the backend does. */
  async resolveDefaultModel(): Promise<string> {
    try {
      const c = (await api.kirocrewConfig()) as KirocrewAgentConfig
      const m = c?.agent?.model || ''
      return m === 'auto' ? '' : m
    } catch {
      return ''
    }
  }

  /** KiroCrew's configured default reasoning effort (Settings → Chat). '' means
   *  no default, i.e. the model picks its own. A per-slot override outranks it,
   *  matching ConfigLoader._acp()'s `reasoning_effort_override or default`. */
  async resolveDefaultEffort(): Promise<string> {
    try {
      const c = (await api.kirocrewConfig()) as KirocrewAgentConfig
      return c?.agent?.reasoning_effort || ''
    } catch {
      return ''
    }
  }

  async fetchUsage(): Promise<NormalizedUsage> {
    const data = await api.kiroUsage()
    const s = data.sessions
    const b = data.billing || {}
    return {
      sessions: {
        total: s.total_sessions,
        today: { sessions: s.today.sessions, messages: s.today.messages, toolCalls: s.today.tool_calls },
        thisWeek: { sessions: s.this_week.sessions, messages: s.this_week.messages, toolCalls: s.this_week.tool_calls },
        thisMonth: { sessions: s.this_month.sessions, messages: s.this_month.messages, toolCalls: s.this_month.tool_calls },
        avgMsgsPerSession: s.avg_msgs_per_session,
        dailyHistory: (s.daily_history || []).map((d: RawDailyHistory) => ({
          date: d.date,
          sessions: d.sessions,
          messages: d.messages,
          toolCalls: d.tool_calls,
        })),
      },
      billing: b.plan ? {
        plan: b.plan,
        used: b.credits_used,
        limit: b.credits_plan,
        unit: 'credits',
        resets: b.resets,
        percentUsed: b.credits_plan ? Math.round((b.credits_used ?? 0) / b.credits_plan * 100) : undefined,
      } : null,
    }
  }

  async fetchProviderHooks(): Promise<Record<string, NormalizedProviderHook[]>> {
    const data = await api.kiroHooks()
    const hooks = data.hooks || {}
    const result: Record<string, NormalizedProviderHook[]> = {}
    for (const [event, entries] of Object.entries(hooks)) {
      result[event] = (entries as RawHookEntry[]).map(e => ({
        event,
        command: e.command,
        matcher: e.matcher,
        source: e.source,
      }))
    }
    return result
  }

  async listPlugins(): Promise<NormalizedPlugin[]> {
    const [agents, skills, mcp] = await Promise.all([
      api.capabilityAgentsList().catch(() => []),
      api.capabilitySkillsList().catch(() => []),
      api.capabilityMcpList().catch(() => []),
    ])
    const plugins: NormalizedPlugin[] = []
    for (const a of agents as RawPlugin[]) {
      plugins.push({ id: a.package || a.name, name: a.name, type: 'agent', source: 'package', version: a.version })
    }
    for (const s of skills as RawPlugin[]) {
      plugins.push({ id: s.package || s.name, name: s.name, type: 'skill', source: 'package', version: s.version })
    }
    for (const m of mcp as RawPlugin[]) {
      plugins.push({ id: m.server_id || m.name, name: m.name, type: 'mcp', source: 'package', version: m.version })
    }
    return plugins
  }

  async installPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp') {
    // The edition capability manager owns any version/source resolution — no
    // version_set is sent. Agent packages are installable via install_agent/
    // uninstall_agent; on an edition without a capability manager the backend
    // answers 503 and this surfaces as a normal error.
    if (type === 'skill') return api.capabilitySkillsInstall(pkg)
    if (type === 'mcp') return api.capabilityMcpInstall(pkg)
    return api.capabilityAgentsInstall(pkg)
  }

  async uninstallPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp') {
    if (type === 'skill') return api.capabilitySkillsUninstall(pkg)
    if (type === 'mcp') return api.capabilityMcpUninstall(pkg)
    return api.capabilityAgentsUninstall(pkg)
  }

  async updatePlugins(_type: 'agent' | 'skill' | 'mcp') {
    // No bulk-update route exists.
    return { ok: false as const, error: 'plugin update is not supported' }
  }

  async fetchAvailableModels(opts?: { slot?: string; scope?: string }): Promise<ModelInfo[]> {
    const sessionScoped = !!opts?.slot
    const markDegraded = (degraded: boolean) => {
      markModelsDegraded(this.id, degraded, opts?.scope)
      // Live-chat display still reads the unscoped getter. A config-namespace
      // 503 must not flip that flag — that is the flap scoped keys stop.
      if (sessionScoped) markModelsDegraded(this.id, degraded)
    }
    try {
      const raw = await api.models(opts?.slot ? { slot: opts.slot } : undefined)
      // TWO shapes, and the shape itself identifies the namespace. The kiro
      // path answers with a BARE ARRAY; a non-kiro backend answers
      // `{models, backend}`. Reading only the array form treated a perfectly
      // good spec-adapter list as a non-array "empty success" and fell through
      // to the cache — serving kiro ids while another backend was active.
      const isBare = Array.isArray(raw)
      const envelope = (raw ?? {}) as {
        models?: unknown
        backend?: unknown
        serves_auto?: unknown
      }
      const models: unknown = isBare ? raw : envelope.models
      const backend: string = isBare ? '' : String(envelope.backend ?? '')
      // A session-scoped fetch must not stamp the config-namespace cache: an
      // open kiro chat listed while the default harness is Codex would
      // otherwise rewrite lastKnown to kiro and flash kiro ids in Settings.
      if (!sessionScoped) {
        rememberBackend(backend)
        // `serves_auto` is only meaningful once a response NAMES a non-kiro
        // backend. An empty `backend` is kiro, which advertises `auto` as a real row
        // — that covers the bare array and equally a malformed object body (a
        // `{error}` from the kiro path carries neither field, and reading its absent
        // flag as a denial would strip kiro's own Auto fallback).
        rememberServesAuto(backend === '' ? true : envelope.serves_auto === true)
      }
      if (!Array.isArray(models) || models.length === 0) {
        // Empty/unusable success: NOT a live list — keep polling, and serve the
        // last-good list only if it belongs to THIS backend's namespace.
        markDegraded(true)
        if (sessionScoped) return []
        return readCachedModels(backend) ?? (servesAutoModel() ? this._defaultModels() : [])
      }
      const result = models.map((m: RawModel) => {
        // Prefer the backend's resolved window over the bundled snapshot: the
        // gateway reads kiro's own --list-models output, so it is right for
        // models this bundle does not list. Learn only the reported value — never
        // the fallback, or a row with no window would record 200K as fact and
        // clobber a window learned from an earlier, better-informed response.
        const reported = rowWindow(m)
        learnWindow(m.model_name, reported)
        return {
          name: m.model_name,
          description: m.description || '',
          contextWindow: reported || MODEL_TOKENS[m.model_name] || DEFAULT_CONTEXT,
          rateMultiplier: rowMultiplier(m),
        }
      })
      if (!sessionScoped) {
        writeCachedModels(result, backend) // good live list, stamped with its namespace
      }
      markDegraded(false) // live success → self-heal can stop polling
      return result
    } catch (e) {
      // Two different failures reach this catch and they need opposite answers.
      //
      // `acp_backend_models_unavailable` is the gateway saying the active
      // backend is NOT kiro and no live session has advertised its namespace
      // yet. Falling back to `auto` there offers an id that backend does not
      // serve: "auto" is a kiro-namespace sentinel the gateway resolves against
      // kiro's advertised set, and a spec adapter rejects it. The operator would
      // pick the only row on offer and the turn would fail at the wire. Serve
      // nothing instead and let the picker render its empty/degraded state — no
      // rows is honest, one unusable row is not.
      //
      // Anything else is a transient backend failure (503 / network / cold
      // start). The cache may still be served there, but ONLY when it is stamped
      // with the namespace that is actually active — otherwise a blip on a spec
      // adapter would resurrect kiro's ids, which is the very confusion the
      // branch above exists to prevent.
      markDegraded(true)
      if (sessionScoped) return []
      const named = backendFromErrorBody(e)
      if (named !== null) rememberBackend(named)
      const affirmed = servesAutoFromErrorBody(e)
      if (affirmed !== null) rememberServesAuto(affirmed)
      if (isBackendNamespaceUnavailable(e)) {
        // No live session has advertised yet. A cache stamped for THIS backend
        // is still its own namespace and stays valid — the refusal is about not
        // knowing the namespace, not about the cache being wrong. Anything
        // stamped for another backend (or unstamped) yields no rows, because a
        // plausible `gpt-5.6-sol` row reads as a real option and is rejected at
        // the wire.
        //
        // `auto` survives here ONLY on an explicit affirmation in this very body,
        // not on `servesAutoModel()`'s remembered default. Reaching this branch is
        // itself proof the backend is not kiro — the gateway emits this code
        // nowhere else — so the absent-means-kiro default is not merely
        // unnecessary here, it would be wrong, and a body from a gateway too old
        // to send the field must keep answering with no rows.
        //
        // The affirmation exists for KAS, which reaches this branch like any other
        // non-kiro backend (it has no local binary to list models) yet does serve
        // `auto`. Deciding by "is the backend id non-empty" stripped the Auto row
        // from a harness that has it; asking whether the backend serves `auto`
        // states the actual requirement (harness-parity H6).
        return readCachedModels(named) ?? (affirmed === true ? this._defaultModels() : [])
      }
      const active = named ?? lastKnownBackend()
      return readCachedModels(active) ?? (servesAutoModel() ? this._defaultModels() : [])
    }
  }

  /** Fallback when the backend model list is unavailable (gateway restart /
   *  kiro-cli cold-start timeout / auth race on /api/models). Exposes ONLY the
   *  "auto" sentinel — never the canonical registry keys (opus-4.8-1m,
   *  fable-5-1m, …). Those keys are DISPLAY identifiers the ACP CLI rejects as
   *  model ids: selecting one during the cold-start window writes it verbatim
   *  into slot.model and kiro-cli fails the turn with -32603 "model not
   *  available". "auto" resolves server-side on the kiro-agent family, so it is
   *  the only safe offering until the real list loads.
   *
   *  Every caller MUST gate on {@link servesAutoModel} first: "auto" is a
   *  kiro-namespace id, not a universal one, and on a spec adapter this list is
   *  the same -32603 trap it exists to prevent. */
  private _defaultModels(): ModelInfo[] {
    return [{
      name: 'auto',
      description: 'Models chosen by task for optimal usage and consistent quality',
      contextWindow: this.getContextWindow('auto'),
    }]
  }

  getContextWindow(model: string): number {
    // Learned-from-backend first, bundled snapshot second, reference default
    // last. This is the value the composer's context meter shows between a model
    // switch and the next turn's live usage_update, so a miss here is what made
    // a 1M model read as 200K until a message was sent.
    return LIVE_WINDOWS[model] ?? MODEL_TOKENS[model] ?? DEFAULT_CONTEXT
  }

  getDefaultModel(): string {
    return 'auto'
  }

  getPermissionModes(): PermissionMode[] {
    return [
      { id: 'normal', label: 'Normal', description: 'Ask before each tool execution' },
      { id: 'trust_reads', label: 'Trust Reads', description: 'Auto-approve read operations, ask for writes' },
      { id: 'trust', label: 'Trust All', description: 'Auto-approve all tool calls' },
      { id: 'yolo', label: 'YOLO', description: 'Skip all approval prompts (dangerous)' },
    ]
  }
}
