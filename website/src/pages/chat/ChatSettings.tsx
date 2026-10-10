/**
 * Chat configuration: the localStorage-backed config shape, its loader/saver,
 * and the dashboard-config type. The settings UI itself lives in
 * pages/settings/ChatPanel.tsx and pages/settings/VoicePanel.tsx.
 */
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { markCompositeFieldsDirty } from '../../lib/uiPrefs'
import { DEFAULT_MESSAGE_FONT_SIZE, MAX_MESSAGE_FONT_SIZE, MIN_MESSAGE_FONT_SIZE } from './contentWidth'

export type ContentWidth = 'compact' | 'comfortable' | 'full'

/* The font-size bounds live in ./contentWidth (with the width scaling that
 * needs them) and are re-exported here so importers of the config module keep
 * one source for everything chat-config shaped. */
export { DEFAULT_MESSAGE_FONT_SIZE, MAX_MESSAGE_FONT_SIZE, MIN_MESSAGE_FONT_SIZE }

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
  /** Pin the most recent prompt above the fold as a sticky banner. */
  pinLastPrompt: boolean
  /** Spellcheck the message composer. When off, the composer input carries
   *  `spellCheck={false}` so the browser draws no red misspelled-word
   *  underlines. Default true — the behaviour every install has always had. */
  spellcheck: boolean
  /** Style bold, italic, strikethrough and inline code in the composer as the
   *  user types. Display only: the markers stay visible and the sent message is
   *  the markdown they typed. A textarea cannot draw styled text, so turning
   *  this on also moves the user onto the Lexical composer (see ChatInput).
   *  Default false: it changes how every draft looks,
   *  so it is the user's call rather than something a client with no stored
   *  config inherits. */
  inlineMarkdown: boolean
  /** Opt in to giving a folder that holds nothing no body at all, so it costs one
   *  row instead of two. Default false: this changes how every empty folder in
   *  the sidebar reads, and the row it removes is the only labelled "New chat in
   *  <name>" affordance those folders have, so it is the user's call rather than
   *  something a client with no stored config inherits. */
  hideEmptyFolderBody: boolean
  /** While the session list scrolls, titles the rows clip are shown in full
   *  in a floating layer over them; rows keep their width (desktop only).
   *  Off by default. */
  fullTitlesOnScroll: boolean
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
  /** Open tool call rows (the per-call pill and the group row around several
   *  calls) expanded instead of collapsed. Initial state only: a row the user
   *  toggled keeps that choice. Default false, so a client with no stored
   *  choice keeps today's collapsed rows (#18254). */
  toolCallsStartExpanded: boolean
  /** Opt in to a double-click on one of your own messages opening the editor
   *  (#7911). Default false: the gesture takes the double-click that would
   *  otherwise select a word in the bubble, so it is the user's call rather
   *  than something a client with no stored config inherits. The pencil button
   *  is the edit path either way. */
  doubleClickToEdit: boolean
  /** Fade split-view panes that do not hold keyboard focus. Default true. When
   *  false every pane stays at full brightness; the focused pane's accent
   *  border still marks where keyboard input goes. */
  dimInactivePanes: boolean
  /** Font size in px for the conversation surface — what the user reads and
   *  writes: message text, inline and block code, tables, follow-up chips and
   *  the composer — clamped to [MIN_MESSAGE_FONT_SIZE, MAX_MESSAGE_FONT_SIZE].
   *  Each element keeps the ratio to body text it has at the default, and the
   *  Compact content width scales with it (see `scaleContentWidth` in ./contentWidth). Chrome —
   *  sidebar, session list, status lines, toolbars — is unaffected, same as
   *  `contentWidth`. */
  messageFontSize: number
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
const DEFAULTS: ChatConfig = { historyExpanded: true, showTimestamps: true, showTurnStats: true, sendOnEnter: 'enter', collapseAllSteps: true, confirmCloseSession: false, simplifiedToolNames: true, contentWidth: 'compact', tagColumnsEnabled: false, fileChipStyle: 'expanded', followUpLayout: 'scroll', streamMode: 'smooth', showContextPct: false, showContextTokens: false, pinLastPrompt: true, hideEmptyFolderBody: false, spellcheck: true, inlineMarkdown: false, showFullPastes: false, toolCallsStartExpanded: false, doubleClickToEdit: false, dimInactivePanes: true, minimapSide: 'left', messageFontSize: DEFAULT_MESSAGE_FONT_SIZE, fullTitlesOnScroll: false }

const clampMessageFontSize = (n: number): number =>
  Math.max(MIN_MESSAGE_FONT_SIZE, Math.min(MAX_MESSAGE_FONT_SIZE, Math.round(n)))

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
    if (typeof cfg.inlineMarkdown !== 'boolean') cfg.inlineMarkdown = false
    // Coerced, not trusted: a stored non-boolean would otherwise make the empty
    // folder shape depend on a truthy string.
    if (typeof cfg.hideEmptyFolderBody !== 'boolean') cfg.hideEmptyFolderBody = false
    if (typeof cfg.fullTitlesOnScroll !== 'boolean') cfg.fullTitlesOnScroll = false
    // Coerced, not trusted: a stored non-boolean would otherwise let a truthy
    // string turn off paste collapsing, which is the main-thread guard for a
    // very large paste.
    if (typeof cfg.showFullPastes !== 'boolean') cfg.showFullPastes = false
    if (typeof cfg.toolCallsStartExpanded !== 'boolean') cfg.toolCallsStartExpanded = false
    // Coerced, not trusted: a stored non-boolean must not attach the
    // double-click gesture that replaces word selection on the bubble.
    if (typeof cfg.doubleClickToEdit !== 'boolean') cfg.doubleClickToEdit = false
    if (typeof cfg.dimInactivePanes !== 'boolean') cfg.dimInactivePanes = true
    if (cfg.minimapSide !== 'left' && cfg.minimapSide !== 'right') cfg.minimapSide = 'left'
    cfg.messageFontSize = typeof cfg.messageFontSize === 'number' ? clampMessageFontSize(cfg.messageFontSize) : DEFAULT_MESSAGE_FONT_SIZE
    return cfg
  }
  catch { return { ...DEFAULTS } }
}

/**
 * Persist the chat config. The marker rollback keeps the stored blob and its
 * dirty markers consistent on a quota-exhausted write (a stored-but-unmarked
 * edit would be mishandled by the sync). Surfacing a FAILED per-field save IS a
 * goal (GPT 6.1 F1, errors-use-error-notice): when the blob or its dirty-marker
 * write cannot be persisted and is rolled back, nothing was stored, so this
 * returns `false` and the caller (`ChatPanel.setChat`) renders the failure
 * through `ErrorNotice` and keeps the prior value on screen rather than showing
 * the un-persisted value as if it saved. A successful or no-op save returns
 * `true`. This is narrower than the pre-existing silent `safeSetItem` behaviour
 * the issue's host-log data ruled out as the #15236 cause; it covers only the
 * per-field save path this change introduces.
 */
export function saveChatConfig(cfg: ChatConfig): boolean {
  // Record which fields this write actually changed, so a deliberate edit --
  // even one that sets a field back to its default value -- is uploaded to the
  // host backup rather than withheld as an unproven default (GPT 5.6 F1). This
  // runs at the single seam every chat-config edit already passes through; it
  // is a marker set here, not interception of the ~300 raw localStorage writers
  // (uiPrefs design decision 2).
  //
  // The comparison basis is the NORMALIZED prior via `loadChatConfig()`, not the
  // raw stored blob: the blob is routinely PARTIAL (restore writers merge only
  // host-held fields, and a build upgrade adds fields the old blob lacks) AND
  // holds un-migrated legacy values, while `cfg` already came through
  // `loadChatConfig()`'s normalizing read (`fileChipStyle:"pebble"` ->
  // `'expanded'`, `sendOnEnter:false` -> `'ctrl-enter'`). Comparing a normalized
  // `cfg` field against a raw legacy `prior` field would mark it dirty on an
  // unrelated toggle and force-upload that untouched field over another origin's
  // host value. `loadChatConfig()` default-fills AND migrates, so a field is
  // dirty only when the user actually changed it (GPT/Opus F1).
  const base = loadChatConfig() as unknown as Record<string, unknown>
  const changed: string[] = []
  for (const [field, value] of Object.entries(cfg)) {
    // Guard the dirty comparison's serialization (GPT 6.1 F1, compare side):
    // `base[field]` comes from the raw stored blob via `loadChatConfig()`,
    // which does not type-check every field, so a hand-tampered / host-backup
    // value nested thousands of arrays deep sits within the store size cap but
    // overflows the JS stack -- a bare `JSON.stringify(base[field])` throws a
    // RangeError here, before the replacement boolean is ever persisted, and
    // the toggle (and the boot path that calls it) dies with no recovery.
    // `value` is always writer-produced (a primitive), so it serializes; treat
    // an unserializable prior value as changed so the serializable replacement
    // is persisted, which is exactly the repair the user is performing.
    let priorStr: string | undefined
    try {
      priorStr = JSON.stringify(base[field])
    } catch {
      changed.push(field)
      continue
    }
    if (priorStr !== JSON.stringify(value)) changed.push(field)
  }
  // Persist ONLY the changed fields merged into the prior RAW blob -- NOT the
  // whole `loadChatConfig()`-default-filled `cfg`. Writing the full default
  // blob materializes all ~21 fields, so `expandComposite` yields 21 child
  // keys with no fingerprints; on a profile that has synced other keys but
  // never stored a chat-config child (any second origin/device's first chat
  // edit after the upgrade) the composite counts as untouched, nothing is
  // withheld, and `buildPatch` uploads every default over newer host values --
  // overwriting e.g. the host's `showTimestamps` with the local default, with
  // no recovery path (GPT 6.1, ChatSettings.tsx:202). Merging only the edited
  // fields into the prior raw blob leaves unedited/absent fields ABSENT, so
  // they stay genuinely unreconciled and are withheld from the flush, while the
  // edited fields are forced up by the dirty marker below. An absent field a
  // reader needs is default-filled on read by `loadChatConfig`, exactly as
  // before -- the default lives in the reader, not the stored blob.
  let priorBlob: Record<string, unknown> = {}
  const priorRaw = safeGetItem(LS_KEY)
  if (priorRaw !== null) {
    try {
      const parsed: unknown = JSON.parse(priorRaw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        priorBlob = parsed as Record<string, unknown>
      }
    } catch {
      /* unparseable prior: start from an empty blob, only edited fields land */
    }
  }
  const nextBlob: Record<string, unknown> = { ...priorBlob }
  const cfgRecord = cfg as unknown as Record<string, unknown>
  for (const field of changed) nextBlob[field] = cfgRecord[field]
  // Exclude any field the raw prior blob carried forward that cannot be
  // serialized (GPT 6.1 F1, save side): a legacy value nested thousands of
  // arrays deep sits within the store's size cap but overflows the JS stack, so
  // a bare JSON.stringify(nextBlob) throws a RangeError and the save -- and the
  // boot path that calls it -- dies before React mounts, with no recovery.
  // Serialize once with a replacer that drops any unserializable field,
  // preserving every valid sibling; a changed field (writer-produced, always a
  // primitive) is never the one dropped.
  let serialized: string
  try {
    serialized = JSON.stringify(nextBlob)
  } catch {
    const safe: Record<string, unknown> = {}
    for (const [field, value] of Object.entries(nextBlob)) {
      try {
        JSON.stringify(value)
      } catch {
        continue
      }
      safe[field] = value
    }
    serialized = JSON.stringify(safe)
  }
  // Write the merged blob FIRST, and mark fields dirty only if that write
  // succeeded (GPT/Opus F2, first half). safeSetItem returns false when quota
  // is exhausted after reclaim; a dirty marker written ahead of a failed blob
  // write would force the UN-updated prior value past buildPatch's withholding
  // and overwrite the host backup -- so no marker is left for a value that was
  // never stored.
  //
  // But the marker is its own ~40-byte key, so the blob write can land while
  // the marker write fails (near-full storage). An edited field recorded in the
  // blob but NOT the marker is then withheld as an unproven default and
  // baselined without uploading, and a cold restore reinstates the stale host
  // value -- the edit is lost silently on both sides (GPT 5.6 F2). So when there
  // ARE changed fields, the marker must persist for the save to be consistent:
  // if it fails, roll the blob back to the prior raw value, leaving nothing
  // stored that the sync would mishandle. The in-memory UI keeps the user's
  // value and the next save retries; a quota wall surfaces as "not saved yet",
  // never as a silent host-value overwrite.
  const wrote = safeSetItem(LS_KEY, serialized)
  if (wrote && changed.length > 0) {
    const marked = markCompositeFieldsDirty(changed)
    if (!marked) {
      // Marker could not be persisted: undo the blob so no stored-but-unmarked
      // edit survives for the sync to upload-or-withhold incorrectly. The
      // in-memory UI keeps the user's value and the next save retries, and the
      // caller is told the save did not persist so it can surface the failure
      // (GPT 6.1 F1, errors-use-error-notice) instead of showing a value that
      // reload will discard.
      if (priorRaw === null) localStorage.removeItem(LS_KEY)
      else safeSetItem(LS_KEY, priorRaw)
      return false
    }
  }
  // The blob write itself failed (quota exhausted even after reclaim): when the
  // user actually changed a field, nothing was stored, so report failure the
  // same way as the marker rollback -- the caller must not display a value that
  // reload will discard (GPT 6.1 F1, errors-use-error-notice). A no-op save (no
  // changed fields) that merely failed to re-write an identical blob loses
  // nothing, so it still reports success.
  if (!wrote && changed.length > 0) {
    window.dispatchEvent(new Event('mc-config-changed'))
    return false
  }
  window.dispatchEvent(new Event('mc-config-changed'))
  return true
}

export interface DashboardConfig {
  restore_sessions: boolean
  restore_window_minutes: number
  merge_queued_messages: boolean
  default_memory_mode: MemoryMode
  widget_density: 'more' | 'less'
  use_builtin_browser: boolean
  verbosity: 'default' | 'concise' | 'answer_only'
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
