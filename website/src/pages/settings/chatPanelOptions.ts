/**
 * Settings ▸ Chat's option tables, numeric bounds and the config shapes its
 * controls read and patch. Plain data and narrowing helpers; `ChatPanel.tsx` and
 * its `useChatPanel*` hooks import from here.
 */
import type { MemoryMode } from '../chat/ChatSettings'
import { ROLE_SLUGS, TECH_SLUGS } from '../../lib/profileOptions'
import { i18nT } from '../../i18n/t'

/**
 * Option labels are FUNCTIONS, not module-level arrays.
 *
 * Every `*_LABELS` array below used to be a module-level const, which is evaluated
 * once at import: an `i18nT()` call there would freeze whatever language was active
 * at boot and never re-resolve. Each resolver is called in the render body instead
 * (`optionLabels={roleLabels()}`), so a language switch re-reads the catalog.
 *
 * Each list stays POSITIONALLY paired with its `*_OPTIONS` array — `SettingsSelect`
 * matches a label to a value by index — so entries must be added and reordered in
 * lockstep.
 */
export const RESTORE_OPTIONS = ['15', '30', '60', '120', '360', '720', '1440', '0']
/** Duration abbreviations are left verbatim (locale-aware unit formatting is Phase 4
 *  territory); only the `'0'` sentinel's label is prose. It reuses the in-chat
 *  settings popover's key — same setting, same option, one string to translate. */
export function restoreLabels(): string[] {
  return ['15m', '30m', '1h', '2h', '6h', '12h', '24h', i18nT('pages.settings.chatPanel.no_limit')]
}
/** How often the feature-video cache readout re-reads while the panel is open. */
export const FEATURE_VIDEO_POLL_MS = 15_000

export const COMPACT_OPTIONS = ['20', '40', '60', '70', '80', '90']
export function compactLabels(): string[] {
  return ['20%', '40%', '60%', `70% (${i18nT('components.jobForm.default')})`, '80%', '90%']
}

// About You — slugs shared with onboarding step 2 and context.py's prompt maps.
export const ROLE_OPTIONS = ['', ...ROLE_SLUGS]
export function roleLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.developer'),
    i18nT('pages.settings.chatPanel.ux_designer'),
    i18nT('pages.settings.chatPanel.product_manager'),
    i18nT('pages.settings.chatPanel.data_ml'),
    i18nT('pages.settings.chatPanel.it_ops'),
    i18nT('pages.settings.chatPanel.other'),
  ]
}
export const TECH_OPTIONS = ['', ...TECH_SLUGS]
export function techLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.i_write_code'),
    i18nT('pages.settings.chatPanel.somewhat'),
    i18nT('pages.settings.chatPanel.not_technical'),
  ]
}

export const SOFT_STOP_MIN = 0.5
export const SOFT_STOP_MAX = 60
export const SOFT_STOP_DEFAULT = 10.0

/** `agent.chat_turn_timeout_secs` bounds and default, mirroring
 *  config/sections.py CHAT_TURN_TIMEOUT_MIN / _MAX and the field default. */
export const TURN_LIMIT_MIN = 300
export const TURN_LIMIT_MAX = 86400
export const TURN_LIMIT_DEFAULT = 14400

/** `dashboard.title_refresh_every_turns`: 0 is the built-in schedule, any other
 *  value is MIN..MAX (config/sections.py TITLE_REFRESH_EVERY_TURNS_MIN / _MAX). */
export const TITLE_REFRESH_PATH = 'dashboard.title_refresh_every_turns'
export const TITLE_REFRESH_MIN = 4
export const TITLE_REFRESH_MAX = 1000

/** The loader's own reading of a cadence: zero or below is the built-in schedule,
 *  1-3 rises to the minimum, anything above the ceiling is capped. A save sends
 *  this value, so what the row shows after a save is what the loader runs: the
 *  PATCH gate stores any 0-1000 and leaves 1-3 to the loader. */
export function normalizeTitleRefresh(n: number): number {
  if (!(n > 0)) return 0
  return Math.min(TITLE_REFRESH_MAX, Math.max(TITLE_REFRESH_MIN, Math.round(n)))
}

export type CompletionKeepMode = 'head' | 'tail' | 'both'
export const COMPLETION_KEEP_OPTIONS: CompletionKeepMode[] = ['head', 'tail', 'both']

export type VerbosityLevel = 'default' | 'concise' | 'answer_only'
export const VERBOSITY_OPTIONS: VerbosityLevel[] = ['default', 'concise', 'answer_only']
/** Retired levels and the level each now means; mirrors the backend's alias map. */
const LEGACY_VERBOSITY: Record<string, VerbosityLevel> = { ultra: 'answer_only' }

export const MEMORY_MODE_OPTIONS: MemoryMode[] = ['persistent', 'incognito', 'temporary']
export const DEFAULT_MEMORY_MODE_PATH = 'dashboardConfig.default_memory_mode'
// The New Session Folders placeholder's example root. A home-relative path reads
// the same on every platform, and the server expands `~` before it validates.
export const FOLDER_ROOT_EXAMPLE = '~/chats'

export function memoryModeLabels(): string[] {
  return [
    i18nT('settings.chat.defaultMemoryMode.persistent'),
    i18nT('components.welcomeView.incognito'),
    i18nT('components.welcomeView.temporary'),
  ]
}

export function asMemoryMode(value: unknown): MemoryMode {
  return MEMORY_MODE_OPTIONS.includes(value as MemoryMode)
    ? value as MemoryMode
    : 'persistent'
}

/**
 * Narrow a persisted `dashboard.verbosity` to a level this Select can render.
 *
 * The config loader reads the field with a plain `.get()` and does not type-check
 * it, so a hand-edited or migrated `config.json` can put any JSON there — e.g.
 * `{"dashboard": {"verbosity": {}}}` — and the GET response hands that object
 * straight to the UI. `?? 'default'` guards only null/undefined, so an object
 * would flow into SimpleSelect's `triggerFallback`
 * (`optionLabels?.[options.indexOf(value)] ?? (value || '—')`): `indexOf` misses,
 * the object is truthy, and React throws on rendering it as a child — taking the
 * whole Chat settings page down rather than degrading one row.
 */
export function asVerbosity(value: unknown): VerbosityLevel {
  if (typeof value === 'string' && Object.prototype.hasOwnProperty.call(LEGACY_VERBOSITY, value)) return LEGACY_VERBOSITY[value]
  return VERBOSITY_OPTIONS.includes(value as VerbosityLevel)
    ? (value as VerbosityLevel)
    : 'default'
}
export function completionKeepLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.head_preserve_start_of_stream'),
    i18nT('pages.settings.chatPanel.tail_preserve_end_final_summary'),
    i18nT('pages.settings.chatPanel.both_head_tail_with_truncation_marker'),
  ]
}
export const COMPLETION_KEEP_CHARS_MIN = 0
// Mirrors RESULT_FILE_MAX_BYTES on the backend (handlers/core.py _EDITABLE_CONFIG).
export const COMPLETION_KEEP_CHARS_MAX = 512000
export const COMPLETION_KEEP_CHARS_DEFAULT = 3000

/** What PATCH returns beside the config when it clamped the value it was sent. */
export type ClampNotice = { requested: number; stored: number; min: number; max: number }

/** Shape of the kirocrewConfig query payload this panel reads and patches. */
export type KirocrewConfigShape = {
  session?: { autocompact_pct?: number; compact_wait_secs?: number }
  session_summary?: { enabled?: boolean }
  agent?: {
    acp_backend?: string
    model?: string
    role_models?: { background?: string; subagent?: string }
    role_efforts?: { background?: string; subagent?: string }
    reasoning_effort?: string
    soft_stop_budget_secs?: number
    chat_turn_timeout_secs?: number
    completion_keep?: CompletionKeepMode
    completion_keep_chars?: number
    fallback_model?: string
    refusal_fallback_model?: string
  }
  dashboard?: { user_role?: string; user_role_other?: string; user_technical_level?: string; prevent_sleep?: boolean; title_refresh_every_turns?: number }
}
