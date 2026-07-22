import { api } from '../../api/client'
import modelTokensRaw from '../../model_tokens.json'
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

/** Raw plugin (agent/skill/mcp) entry from /api/aim/*. */
interface RawAimPlugin {
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
      api.aimAgentsList().catch(() => []),
      api.aimSkillsList().catch(() => []),
      api.aimMcpList().catch(() => []),
    ])
    const plugins: NormalizedPlugin[] = []
    for (const a of agents as RawAimPlugin[]) {
      plugins.push({ id: a.package || a.name, name: a.name, type: 'agent', source: 'aim', version: a.version })
    }
    for (const s of skills as RawAimPlugin[]) {
      plugins.push({ id: s.package || s.name, name: s.name, type: 'skill', source: 'aim', version: s.version })
    }
    for (const m of mcp as RawAimPlugin[]) {
      plugins.push({ id: m.server_id || m.name, name: m.name, type: 'mcp', source: 'aim', version: m.version })
    }
    return plugins
  }

  async installPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp', versionSet?: string) {
    if (type === 'agent') return api.aimAgentsInstall(pkg, versionSet)
    if (type === 'skill') return api.aimSkillsInstall(pkg, versionSet)
    return api.aimMcpInstall(pkg)
  }

  async uninstallPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp') {
    if (type === 'agent') return api.aimAgentsUninstall(pkg)
    if (type === 'skill') return api.aimSkillsUninstall(pkg)
    return api.aimMcpUninstall(pkg)
  }

  async updatePlugins(type: 'agent' | 'skill' | 'mcp') {
    return api.aimUpdate(type === 'mcp' ? 'mcp' : type === 'skill' ? 'skills' : 'agents')
  }

  async fetchAvailableModels(): Promise<ModelInfo[]> {
    try {
      const models = await api.models()
      if (!Array.isArray(models)) return this._defaultModels()
      const result = models.map((m: RawModel) => ({
        name: m.model_name,
        description: m.description || '',
        contextWindow: MODEL_TOKENS[m.model_name] ?? DEFAULT_CONTEXT,
      }))
      return result.length > 0 ? result : this._defaultModels()
    } catch {
      return this._defaultModels()
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
