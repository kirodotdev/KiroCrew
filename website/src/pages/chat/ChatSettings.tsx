/**
 * Chat configuration: the localStorage-backed config shape, its loader/saver,
 * and the dashboard-config type. The settings UI itself lives in
 * pages/settings/ChatPanel.tsx and pages/settings/VoicePanel.tsx.
 */
import { safeSetItem } from '../../utils/safeStorage'

export type ContentWidth = 'compact' | 'comfortable' | 'full'

/** Send-key mode: enter (Enter sends), ctrl-enter (Ctrl+Enter sends), enter-ctrl-newline (Enter sends, Ctrl+Enter = newline) */
export type SendMode = 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'

export type MemoryMode = 'persistent' | 'incognito' | 'temporary'

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
  /** Show used/window token counts in the inline context readout. */
  showContextTokens: boolean
  defaultAutopilot: boolean
  /** Pin the most recent prompt above the fold as a sticky banner. */
  pinLastPrompt: boolean
  /** Spellcheck the message composer. When off, the composer input carries
   *  `spellCheck={false}` so the browser draws no red misspelled-word
   *  underlines. Default true — the behaviour every install has always had. */
  spellcheck: boolean
  /** Opt in to giving a folder that holds nothing no body at all, so it costs one
   *  row instead of two. Default false: this changes how every empty folder in
   *  the sidebar reads, and the row it removes is the only labelled "New chat in
   *  <name>" affordance those folders have, so it is the user's call rather than
   *  something a client with no stored config inherits. */
  hideEmptyFolderBody: boolean
  /** Which pane edge hosts the turn minimap. The right-edge variant replaces
   *  the native scrollbar while the rail is shown. */
  minimapSide: MinimapSide
  /** Keep a long paste as full editable text in the composer instead of
   *  collapsing it into a `[ Paste #N · M lines ]` chip. Default false: the chip
   *  is what keeps the composer (and the sent bubble) from laying out a
   *  hundred-thousand-line paste on the main thread, so the full-text shape is
   *  the user's call rather than something a client with no stored config
   *  inherits. Cmd/Ctrl+Shift+V remains the per-paste escape hatch either way. */
  showFullPastes: boolean
}

export type FileChipStyle = 'expanded' | 'minimal'
export type FollowUpLayout = 'multiline' | 'scroll'
export type MinimapSide = 'left' | 'right'
/** Per-char streaming entrance animation. 'immediate' restores the pre-buffer
 *  behavior (raw chunk paint + tail glow only). */
export type StreamMode = 'immediate' | 'smooth'

const LS_KEY = 'mc-chat-config'
/** `tagColumnsEnabled` MUST default to false: board-vs-list is derived from
 *  this client-only flag AND the server-side column list, so a default of true
 *  means any client with no stored config (a new user, a second browser, a
 *  fresh Electron profile, a synced instance that inherited tag_boards.json,
 *  or a client whose quota-safe write was dropped) opens straight into board
 *  view the moment one column exists on the gateway — without anyone choosing
 *  it. The sidebar's view toggle persists this flag BEFORE creating its first
 *  column, so a deliberate board user always has an explicit `true` stored and
 *  is unaffected by the default. */
const DEFAULTS: ChatConfig = { historyExpanded: true, showTimestamps: true, showTurnStats: true, sendOnEnter: 'enter', collapseAllSteps: true, confirmCloseSession: false, simplifiedToolNames: true, contentWidth: 'compact', tagColumnsEnabled: false, fileChipStyle: 'expanded', followUpLayout: 'scroll', streamMode: 'smooth', showContextPct: false, showContextTokens: false, defaultAutopilot: false, pinLastPrompt: true, hideEmptyFolderBody: false, spellcheck: true, showFullPastes: false, minimapSide: 'left' }

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
    if (typeof cfg.showContextTokens !== 'boolean') cfg.showContextTokens = false
    if (typeof cfg.showTurnStats !== 'boolean') cfg.showTurnStats = true
    if (typeof cfg.pinLastPrompt !== 'boolean') cfg.pinLastPrompt = true
    // Coerced, not trusted: a stored non-boolean must not decide whether the
    // composer draws the browser's red spellcheck underlines.
    if (typeof cfg.spellcheck !== 'boolean') cfg.spellcheck = true
    // Coerced, not trusted: a stored non-boolean would otherwise make the empty
    // folder shape depend on a truthy string.
    if (typeof cfg.hideEmptyFolderBody !== 'boolean') cfg.hideEmptyFolderBody = false
    // Coerced, not trusted: a stored non-boolean would otherwise let a truthy
    // string turn off paste collapsing, which is the main-thread guard for a
    // very large paste.
    if (typeof cfg.showFullPastes !== 'boolean') cfg.showFullPastes = false
    if (cfg.minimapSide !== 'left' && cfg.minimapSide !== 'right') cfg.minimapSide = 'left'
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
  default_memory_mode: MemoryMode
  widget_density: 'more' | 'less'
  use_builtin_browser: boolean
  verbosity: 'default' | 'concise' | 'ultra' | 'answer_only'
  quick_send: boolean
  session_grid: boolean
  tail_fork_enabled: boolean
  link_previews: boolean
  link_patterns: { pattern: string; url: string }[]
  mcp_app_panel: boolean
  auto_open_git_panel: boolean
  session_card_source_links: boolean
  folder_suggestions_enabled: boolean
  model_picker_hidden_models: string[]
  model_picker_configured?: boolean
}
