// KiroCrew is KiroACP-only — kiro-cli over ACP is the sole provider. The
// ClaudeCode/Bedrock providers were deleted; this stays a single-member union
// so the adapter interface below still type-checks for its many consumers.
export type ProviderId = 'acp'

export interface ProviderCapabilities {
  hooks: boolean
  pluginRegistry: boolean
  agentTemplates: boolean
  usageBilling: boolean
  warmPool: boolean
  modelResolution: boolean
  toolExecution: boolean
  subagents: boolean
  sandbox: boolean
  contextWindow: boolean
  modelSwitching: boolean
  permissionModes: boolean
  sessionResume: boolean
  compaction: boolean
  reasoningEffort: boolean
}

export interface UsagePeriod {
  sessions: number
  messages: number
  toolCalls: number
}

export interface TokenBreakdown {
  input: number
  output: number
  cacheCreation: number
  cacheRead: number
  total: number
}

export interface NormalizedUsage {
  sessions: {
    total: number
    today: UsagePeriod
    thisWeek: UsagePeriod
    thisMonth: UsagePeriod
    avgMsgsPerSession: number
    dailyHistory: { date: string; sessions: number; messages: number; toolCalls: number }[]
  }
  billing: {
    plan?: string
    used?: number
    limit?: number
    unit: 'credits' | 'usd' | 'tokens'
    resets?: string
    percentUsed?: number
  } | null
  tokens?: TokenBreakdown
  costUsd?: number
  totalTurns?: number
  totalDurationMs?: number
  tokenDailyHistory?: {
    date: string
    input: number
    output: number
    cacheCreate: number
    cacheRead: number
    costUsd: number
    models?: Record<string, { input: number; output: number; cacheCreate: number; cacheRead: number; costUsd: number }>
    providers?: Record<string, { input: number; output: number; cacheCreate: number; cacheRead: number; costUsd: number }>
    providerModels?: Record<string, Record<string, { input: number; output: number; cacheCreate: number; cacheRead: number; costUsd: number }>>
  }[]
  tokenProviders?: string[]
  tokenModels?: string[]
  tokenProviderModels?: Record<string, string[]>
}

export interface NormalizedProviderHook {
  event: string
  command: string
  matcher?: string
  source?: string
}

export interface NormalizedPlugin {
  id: string
  name: string
  type: 'agent' | 'skill' | 'mcp'
  source: string
  version?: string
}

export interface AgentBinding {
  name: string
  kiro_agent?: string
  workspace?: string
  memory_store?: string
}

export interface ProviderLabels {
  sessionProcess: string
  agentTemplateField: string
  processCountLabel: string
  warmPoolDescription: string
  configFile: string
  pluginRegistryName: string
  hooksSection: string
}

export interface ModelInfo {
  name: string
  description: string
  contextWindow?: number
  supportsExtendedContext?: boolean
}

export interface PermissionMode {
  id: string
  label: string
  description: string
}

export interface ProviderAdapter {
  readonly id: ProviderId
  readonly displayName: string
  readonly capabilities: ProviderCapabilities
  readonly labels: ProviderLabels

  resolveAgentTemplate(agent: AgentBinding): string
  resolveModel(templateName: string): Promise<string>
  /** The provider-level default model for NEW sessions ('' when none is set).
   *  Distinct from resolveModel, which resolves a specific agent template. */
  resolveDefaultModel(): Promise<string>
  /** The provider-level default reasoning effort for NEW sessions ('' = none,
   *  i.e. let the model choose). A per-session override always outranks it. */
  resolveDefaultEffort(): Promise<string>

  fetchUsage(): Promise<NormalizedUsage>

  fetchProviderHooks(): Promise<Record<string, NormalizedProviderHook[]>>

  listPlugins(): Promise<NormalizedPlugin[]>
  installPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp'): Promise<{ ok: boolean; error?: string }>
  uninstallPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp'): Promise<{ ok: boolean; error?: string }>
  updatePlugins(type: 'agent' | 'skill' | 'mcp'): Promise<{ ok: boolean; output?: string; error?: string }>

  fetchAvailableModels(): Promise<ModelInfo[]>
  getContextWindow(model: string): number
  getDefaultModel(): string
  getPermissionModes(): PermissionMode[]
}
