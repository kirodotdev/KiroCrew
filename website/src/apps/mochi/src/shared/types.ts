/**
 * Mochi - Core shared types
 */

// ── Pet State Machine ──────────────────────────────────────────────────────

export type PetState =
  | 'idle'
  | 'thinking'
  | 'working'
  | 'walking'
  | 'error'
  | 'offline'

export type PetMood = 'neutral' | 'happy' | 'sleepy' | 'curious' | 'busy' | 'scared'

export type PetEvent =
  | 'user_input'
  | 'voice_start'
  | 'voice_end'
  | 'voice_error'
  | 'task_start'
  | 'tool_call'
  | 'task_complete'
  | 'approval_required'
  | 'approval_granted'
  | 'approval_rejected'
  | 'walk_start'
  | 'walk_done'
  | 'error'
  | 'connect'
  | 'disconnect'
  | 'timeout'

// ── Agent Backend Interface ────────────────────────────────────────────────

export type TaskID = string

export type TaskStatus = 'pending' | 'running' | 'done' | 'error' | 'cancelled'

export interface AgentContext {
  appName?: string
  windowTitle?: string
  focusedText?: string
  clipboardContent?: string
}

export interface NotificationPayload {
  taskId: string
  summary: string       // max 100 chars
  completedAt: number   // Unix timestamp ms
  fullResult?: string
}

export interface Briefing {
  items: BriefingItem[]
  generatedAt: number
}

export interface BriefingItem {
  title: string
  description: string
  priority: 'high' | 'medium' | 'low'
}

export interface Intent {
  type: 'task' | 'query' | 'approve' | 'cancel' | 'chitchat'
  target?: string
  instruction: string
  screenshot?: string   // base64
  context?: AgentContext
  confidence: number    // 0-1
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  screenshot?: string   // base64, if attached
  backfill?: boolean    // true for messages backfilled from dashboard (don't trigger waiting state)
}

export interface ApprovalRequest {
  id: string
  taskId: string
  toolName: string
  paramsSummary: string
  riskLevel: 'low' | 'medium' | 'high'
}

// ── PetAgentBackend interface ──────────────────────────────────────────────

export interface PetAgentBackend {
  /** Send a task (with optional screenshot) to the agent backend */
  sendTask(
    screenshot: string | null,
    instruction: string,
    context: AgentContext
  ): Promise<TaskID>

  /** Stream task status updates */
  onTaskUpdate(
    callback: (status: TaskStatus, progress: string, result?: string) => void
  ): () => void  // returns unsubscribe fn

  /** Receive proactive notifications from agent */
  onProactiveNotification(
    callback: (message: NotificationPayload) => void
  ): () => void

  /** Receive approval requests from agent */
  onApprovalRequired(
    callback: (request: ApprovalRequest) => void
  ): () => void

  /** Get daily briefing */
  getDailyBriefing(): Promise<Briefing>

  /** Approve or reject an agent action */
  approveAction(taskId: string, approved: boolean): Promise<void>

  /** Check backend health */
  checkHealth(): Promise<boolean>
}

// ── IPC channel names ──────────────────────────────────────────────────────

export const IPC = {
  PET_STATE_CHANGE: 'pet:state-change',
  CHAT_MESSAGE: 'chat:message',
  CHAT_SEND: 'chat:send',
  CHAT_HISTORY: 'chat:history',
  NOTIFICATION_SHOW: 'notification:show',
  APPROVAL_REQUEST: 'approval:request',
  APPROVAL_RESPOND: 'approval:respond',
  CONFIG_GET: 'config:get',
  CONFIG_UPDATE: 'config:update',
  CONFIG_RESET: 'config:reset',
  CAPTURE_START: 'capture:start',
  CAPTURE_DONE: 'capture:done',
  CAPTURE_CANCEL: 'capture:cancel',
  VOICE_START: 'voice:start',
  VOICE_STOP: 'voice:stop',
  VOICE_RESULT: 'voice:result',
  CLIPBOARD_PROMPT: 'clipboard:prompt',
  BACKEND_STATUS: 'backend:status',
  GALLERY_LIST_PACKS: 'gallery:list-packs',
  GALLERY_GET_PACK_DETAIL: 'gallery:get-pack-detail',
  GALLERY_GET_ANIMATION: 'gallery:get-animation',
  GALLERY_SET_ACTIVE: 'gallery:set-active',
  GALLERY_IMPORT: 'gallery:import',
  GALLERY_DELETE: 'gallery:delete',
  GALLERY_SAVE_PACK: 'gallery:save-pack',
  GALLERY_EXPORT: 'gallery:export',
  GALLERY_IMPORT_BUNDLE: 'gallery:import-bundle',
  GALLERY_ACTIVE_CHANGED: 'gallery:active-changed',
  GALLERY_PACKS_CHANGED: 'gallery:packs-changed',
  GALLERY_OPEN: 'gallery:open',
  GALLERY_SET_COLOR_MAP: 'gallery:set-color-map',
  GALLERY_COLOR_MAP_CHANGED: 'gallery:color-map-changed',
  PRESETS_SAVE_CUSTOM: 'presets:save-custom',
  PRESETS_LOAD_CUSTOM: 'presets:load-custom',
  PRESETS_GET_COLOR_MAP: 'presets:get-color-map',
  DICTATION_TOGGLE: 'dictation:toggle',
  DICTATION_CANCEL: 'dictation:cancel',
  DICTATION_STATE: 'dictation:state',
  DICTATION_AUDIO_LEVEL: 'dictation:audio-level',
  DICTATION_TRANSCRIPT: 'dictation:transcript',
  DICTATION_STOP_RECORDING: 'dictation:stop-recording',
  DICTATION_RENDERER_TRANSCRIPT: 'dictation:renderer-transcript',
  DICTATION_RENDERER_AUDIO_LEVEL: 'dictation:renderer-audio-level',
  DICTATION_SEND: 'dictation:send',
  DICTATION_ERROR: 'dictation:error',
  DICTATION_STT_UNAVAILABLE: 'dictation:stt-unavailable',
} as const

// ── Companion Stats ────────────────────────────────────────────────────────

export interface CompanionStats {
  firstLaunch: string                        // ISO 8601
  streak: number                             // consecutive launch days
  lastActiveDate: string                     // YYYY-MM-DD
  companionSeconds: number                   // cumulative app-open seconds
  messages: { sent: number; received: number }
  walkSteps: number
  screenshots: number
  peeks: number
  drags: number
  thinkingSeconds: number
  latestActiveTime: string                   // HH:mm
  earliestActiveTime: string                 // HH:mm
  moods: Record<string, number>
  longestChat: number
  busiestDay: { date: string; messages: number }
  lastMemoryHour: number                     // last companion hour written to activity log
}

/** Helper: today as YYYY-MM-DD */
function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Factory — returns a fresh default CompanionStats (never shared reference) */
export function createDefaultStats(): CompanionStats {
  return {
    firstLaunch: new Date().toISOString(),
    streak: 1,
    lastActiveDate: todayStr(),
    companionSeconds: 0,
    messages: { sent: 0, received: 0 },
    walkSteps: 0,
    screenshots: 0,
    peeks: 0,
    drags: 0,
    thinkingSeconds: 0,
    latestActiveTime: '',
    earliestActiveTime: '',
    moods: {},
    longestChat: 0,
    busiestDay: { date: '', messages: 0 },
    lastMemoryHour: 0,
  }
}

/**
 * Safely parse a raw JSON string into CompanionStats.
 * Returns valid defaults (never throws) for any corrupted/invalid input.
 */
export function parseStatsJSON(raw: string): CompanionStats {
  try {
    const parsed = JSON.parse(raw) as Partial<CompanionStats>
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return createDefaultStats()
    }
    return mergeStats(createDefaultStats(), parsed)
  } catch {
    return createDefaultStats()
  }
}

/**
 * Merge partial/loaded stats onto defaults, filling any missing fields.
 * Similar to mergeConfig but for CompanionStats.
 */
export function mergeStats(
  base: CompanionStats,
  overrides: Partial<CompanionStats>,
): CompanionStats {
  return {
    firstLaunch: overrides.firstLaunch ?? base.firstLaunch,
    streak: overrides.streak ?? base.streak,
    lastActiveDate: overrides.lastActiveDate ?? base.lastActiveDate,
    companionSeconds: overrides.companionSeconds ?? base.companionSeconds,
    messages: { ...base.messages, ...overrides.messages },
    walkSteps: overrides.walkSteps ?? base.walkSteps,
    screenshots: overrides.screenshots ?? base.screenshots,
    peeks: overrides.peeks ?? base.peeks,
    drags: overrides.drags ?? base.drags,
    thinkingSeconds: overrides.thinkingSeconds ?? base.thinkingSeconds,
    latestActiveTime: overrides.latestActiveTime ?? base.latestActiveTime,
    earliestActiveTime: overrides.earliestActiveTime ?? base.earliestActiveTime,
    moods: overrides.moods ?? base.moods,
    longestChat: overrides.longestChat ?? base.longestChat,
    busiestDay: { ...base.busiestDay, ...overrides.busiestDay },
    lastMemoryHour: overrides.lastMemoryHour ?? base.lastMemoryHour,
  }
}

// ── MCP Server Info (shared between main and renderer) ─────────────────────

/** Per-agent effective tool policy (after preset merge + BG filtering). */
export interface AgentToolPolicy {
  autoApprove: string[]
  disabledTools: string[]
}

/** Enriched MCP server info for Settings UI. */
export interface McpServerInfo {
  name: string
  core: boolean
  enabled: boolean
  agents: ('chat' | 'bg')[]
  autoApprove: string[]       // user-level overrides (raw config)
  disabledTools: string[]     // user-level overrides (raw config)
  chatPolicy: AgentToolPolicy // effective policy for chat agent
  bgPolicy: AgentToolPolicy   // effective policy for bg agent
  toolCount?: number
}
