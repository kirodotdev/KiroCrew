/**
 * Chat configuration: the localStorage-backed config shape, its loader/saver,
 * and the dashboard-config type. The settings UI itself lives in
 * pages/settings/ChatPanel.tsx and pages/settings/VoicePanel.tsx.
 */
import { safeSetItem } from '../../utils/safeStorage'

export type ContentWidth = 'compact' | 'comfortable' | 'full'

/** Send-key mode: enter (Enter sends), ctrl-enter (Ctrl+Enter sends), enter-ctrl-newline (Enter sends, Ctrl+Enter = newline) */
export type SendMode = 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'

export const CONTENT_WIDTH: Record<ContentWidth, { messages: string; input: string }> = {
  compact: { messages: '800px', input: '816px' },
  comfortable: { messages: '84%', input: '85%' },
  // 'full' = the widest single-pane width (keeps a small gutter so text doesn't
  // touch the window edge). Native grid panes force true edge-to-edge (100%)
  // via ChatPane's inline --mc-content-width, so this global constant stays at
  // the single-pane value — widening it here would silently change single-pane
  // "full" users who never opted into Split View.
  full: { messages: '92%', input: '93%' },
}

export interface ChatConfig {
  contentWidth: ContentWidth
  historyExpanded: boolean
  showTimestamps: boolean
  showTurnStats: boolean
  sendOnEnter: SendMode
  collapseAllSteps: boolean
  confirmCloseSession: boolean
  simplifiedToolNames: boolean
  tagColumnsEnabled: boolean
  fileChipStyle: FileChipStyle
  followUpLayout: FollowUpLayout
  streamMode: StreamMode
  showContextPct: boolean
  defaultAutopilot: boolean
  /** Pin the most recent prompt above the fold as a sticky banner. */
  pinLastPrompt: boolean
}

export type FileChipStyle = 'expanded' | 'minimal'
export type FollowUpLayout = 'multiline' | 'scroll'
/** Per-char streaming entrance animation. 'immediate' restores the pre-buffer
 *  behavior (raw chunk paint + tail glow only). */
export type StreamMode = 'immediate' | 'smooth'

const LS_KEY = 'mc-chat-config'
const DEFAULTS: ChatConfig = { historyExpanded: true, showTimestamps: true, showTurnStats: true, sendOnEnter: 'enter', collapseAllSteps: true, confirmCloseSession: false, simplifiedToolNames: true, contentWidth: 'compact', tagColumnsEnabled: true, fileChipStyle: 'expanded', followUpLayout: 'scroll', streamMode: 'smooth', showContextPct: false, defaultAutopilot: false, pinLastPrompt: true }

const VALID_FILE_CHIP_STYLES: ReadonlySet<FileChipStyle> = new Set(['expanded', 'minimal'])
const VALID_FOLLOW_UP_LAYOUTS: ReadonlySet<FollowUpLayout> = new Set(['multiline', 'scroll'])
const VALID_STREAM_MODES: ReadonlySet<StreamMode> = new Set(['immediate', 'smooth'])

/** Migrate legacy boolean sendOnEnter to new SendMode enum */
function migrateSendMode(raw: unknown): SendMode {
  if (raw === true) return 'enter'
  if (raw === false) return 'ctrl-enter'
  if (raw === 'enter' || raw === 'ctrl-enter' || raw === 'enter-ctrl-newline') return raw
  return 'enter'
}

export function loadChatConfig(): ChatConfig {
  try {
    const stored = JSON.parse(localStorage.getItem(LS_KEY) || '{}')
    const cfg = { ...DEFAULTS, ...stored, sendOnEnter: migrateSendMode(stored.sendOnEnter) }
    if (!(cfg.contentWidth in CONTENT_WIDTH)) cfg.contentWidth = 'compact'
    // Map legacy fileChipStyle values onto the current set:
    //   'tooltip'                                 → 'minimal'
    //   'pebble' / 'full' / 'compact'             → 'expanded'
    //   'expanded-aurora' / 'expanded-domed'      → 'expanded'
    const legacy = cfg.fileChipStyle as string
    if (legacy === 'tooltip') cfg.fileChipStyle = 'minimal'
    else if (legacy === 'pebble' || legacy === 'full' || legacy === 'compact'
          || legacy === 'expanded-aurora' || legacy === 'expanded-domed') cfg.fileChipStyle = 'expanded'
    if (!VALID_FILE_CHIP_STYLES.has(cfg.fileChipStyle)) cfg.fileChipStyle = 'expanded'
    if (!VALID_FOLLOW_UP_LAYOUTS.has(cfg.followUpLayout)) cfg.followUpLayout = 'scroll'
    if (!VALID_STREAM_MODES.has(cfg.streamMode)) cfg.streamMode = 'smooth'
    if (typeof cfg.showContextPct !== 'boolean') cfg.showContextPct = false
    if (typeof cfg.showTurnStats !== 'boolean') cfg.showTurnStats = true
    if (typeof cfg.pinLastPrompt !== 'boolean') cfg.pinLastPrompt = true
    return cfg
  }
  catch { return { ...DEFAULTS } }
}

export function saveChatConfig(cfg: ChatConfig) {
  safeSetItem(LS_KEY, JSON.stringify(cfg))
  window.dispatchEvent(new Event('mc-config-changed'))
}

export interface DashboardConfig {
  restore_sessions: boolean
  restore_window_minutes: number
  merge_queued_messages: boolean
  widget_density: 'more' | 'less'
  verbosity: 'default' | 'concise' | 'ultra'
  quick_send: boolean
  session_grid: boolean
  tail_fork_enabled: boolean
  link_previews: boolean
  mcp_app_panel: boolean
  folder_suggestions_enabled: boolean
}
