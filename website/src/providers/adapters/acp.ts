import { api } from '../../api/client'
import modelTokensRaw from '../../model_tokens.json'
import { markModelsDegraded } from '../modelListHealth'
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

  async resolveModel(templateName: string): Promise<string> {
    try {
      const d = await api.agentDetail(templateName)
      return d?.model || ''
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
    // Package (agent) install/uninstall/update routes were removed; only
    // skill + MCP install remain (Kiro-only apply). The edition capability
    // manager owns any version/source resolution — no version_set is sent.
    if (type === 'skill') return api.capabilitySkillsInstall(pkg)
    if (type === 'mcp') return api.capabilityMcpInstall(pkg)
    return { ok: false as const, error: 'agent install is not supported' }
  }

  async uninstallPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp') {
    if (type === 'skill') return api.capabilitySkillsUninstall(pkg)
    if (type === 'mcp') return api.capabilityMcpUninstall(pkg)
    return { ok: false as const, error: 'agent uninstall is not supported' }
  }

  async updatePlugins(_type: 'agent' | 'skill' | 'mcp') {
    // Bulk update route was removed.
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
      const result = models.map((m: RawModel) => ({
        name: m.model_name,
        description: m.description || '',
        contextWindow: MODEL_TOKENS[m.model_name] ?? DEFAULT_CONTEXT,
      }))
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
   *  model ids: selecting one during the cold-start window wrote it verbatim
   *  into slot.model and kiro-cli failed the turn with -32603 "model not
   *  available". "auto" always resolves server-side, so it is the only safe
   *  offering until the real list loads. */
  private _defaultModels(): ModelInfo[] {
    return [{
      name: 'auto',
      description: 'Models chosen by task for optimal usage and consistent quality',
      contextWindow: MODEL_TOKENS['auto'] ?? DEFAULT_CONTEXT,
    }]
  }

  getContextWindow(model: string): number {
    return MODEL_TOKENS[model] ?? DEFAULT_CONTEXT
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
