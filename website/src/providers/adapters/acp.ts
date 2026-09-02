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
}

/** Read the last-good live model list from localStorage, or null if absent,
 *  expired (older than the TTL), or unusable. Fully guarded: SSR (no
 *  localStorage), disabled storage, quota, and corrupt JSON all degrade to null
 *  so the caller falls through to auto-only. */
function readCachedModels(): ModelInfo[] | null {
  try {
    if (typeof localStorage === 'undefined') return null
    const raw = localStorage.getItem(MODELS_CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachedModels
    if (!parsed || typeof parsed.ts !== 'number' || !Array.isArray(parsed.models)) return null
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
for (const m of readCachedModels() ?? []) learnWindow(m.name, m.contextWindow ?? 0)

/** Persist a live model list with a timestamp. Best-effort — storage errors
 *  (quota, disabled, SSR) are swallowed so caching never breaks the picker. */
function writeCachedModels(models: ModelInfo[]): void {
  try {
    if (typeof localStorage === 'undefined') return
    const payload: CachedModels = { ts: Date.now(), models }
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

  async fetchAvailableModels(): Promise<ModelInfo[]> {
    try {
      const models = await api.models()
      if (!Array.isArray(models) || models.length === 0) {
        // Empty/non-array success: NOT a live list — keep polling, serve the
        // last-good live list if we have one, else auto-only.
        markModelsDegraded(this.id, true)
        return readCachedModels() ?? this._defaultModels()
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
      writeCachedModels(result) // remember this good live list for next hiccup
      markModelsDegraded(this.id, false) // live success → self-heal can stop polling
      return result
    } catch {
      // Transient backend failure (503 / network): NOT live — keep polling.
      // Serve the last-good live list if we have one, else auto-only. Never
      // surface canonical registry keys — the ACP CLI rejects them (-32603).
      markModelsDegraded(this.id, true)
      return readCachedModels() ?? this._defaultModels()
    }
  }

  /** Fallback when the backend model list is unavailable (gateway restart /
   *  kiro-cli cold-start timeout / auth race on /api/models). Exposes ONLY the
   *  "auto" sentinel — never the canonical registry keys (opus-4.8-1m,
   *  fable-5-1m, …). Those keys are DISPLAY identifiers the ACP CLI rejects as
   *  model ids: selecting one during the cold-start window writes it verbatim
   *  into slot.model and kiro-cli fails the turn with -32603 "model not
   *  available". "auto" always resolves server-side, so it is the only safe
   *  offering until the real list loads. */
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
