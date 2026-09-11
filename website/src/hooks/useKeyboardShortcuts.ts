import { useEffect, useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../store'
import { switchSlot, deleteSlot, openActivityToTab, selectSidebarSubagentCounts, selectSidebarApprovalCounts, selectSidebarWorkflowActive, selectSidebarAutomationRunningKeys } from '../store/chatSlice'
import { inferLane } from '../pages/chat/sessionLane'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { queryComposerOrExpand, queryPendingApprovalAction, releaseComposerForKeyboardSwitch } from '../pages/chat/composerFocus'
import { reportSeamCollision } from '../apps/seamCollision'
import {
  loadPanelToggleOverrides,
  matchPanelToggleEvent,
  PANEL_TOGGLE_SHORTCUTS_EVENT,
  PANEL_TOGGLE_SHORTCUTS_KEY,
  PANEL_TOGGLES_SKIPPING_SHELL,
  type PanelToggleId,
  type PanelToggleOverrides,
} from '../lib/panelToggleShortcuts'
import {
  SHORTCUT_GROUPS,
  SHORTCUT_OVERRIDES_EVENT,
  SHORTCUT_OVERRIDES_KEY,
  SHORTCUT_REGISTRY,
  chordMatchesEvent,
  loadShortcutOverrides,
  matchShortcutEvent,
  resolveShortcuts,
  shortcutEntry,
  type Chord,
  type ResolvedShortcut,
  type ShortcutEntry,
  type ShortcutGroup,
  type ShortcutOverrides,
  type ShortcutPlatform,
} from '../lib/shortcutRegistry'
import { i18nT } from '../i18n/t'
import { canGoBack, canGoForward } from '../lib/routeHistoryPosition'
import { useGuardedHistoryStep } from '../components/NavigationLeaveGuard'
import { MOBILE_BREAKPOINT } from './useIsMobile'

/**
 * Group ids + ordering live in the registry (`lib/shortcutRegistry`); re-exported
 * here because every display surface imports them from this hook.
 */
export { SHORTCUT_GROUPS, type ShortcutGroup }

export const SHORTCUTS_ENABLED_KEY = 'mc-keyboard-shortcuts'
export const SHORTCUTS_ENABLED_EVENT = 'mc-keyboard-shortcuts-changed'
export const MAC_CTRL_DIGITS_KEY = 'mc-mac-ctrl-digits'

/** True on macOS where Option+number produces characters (UK: ⌥3→#, ⌥2→€). */
function isMacPlatform(): boolean {
  return /Mac|iPhone|iPad/.test(navigator?.platform ?? '') || /Macintosh/.test(navigator?.userAgent ?? '')
}
// Frozen at module load for the chord-shape decisions (modifier choice, gate
// branches) — platform never changes at runtime. Call sites that must be
// exercisable under the test suite's setPlatform() convention read
// isMacPlatform() live instead.
export const IS_MAC = isMacPlatform()

/** Whether Mac uses Ctrl+digit (true, default) or Alt+digit (false, legacy). */
export function getCtrlDigitsEnabled(): boolean {
  return IS_MAC && localStorage.getItem(MAC_CTRL_DIGITS_KEY) !== '0'
}

/**
 * Slots in the order the chat-jump and chat-cycle shortcuts should walk them:
 * the sidebar's DISPLAYED order (`dashboard.sidebarOrder`, pinned-first + the
 * user's sort), not the store array (backend insertion order). Ctrl+1 must hit
 * the row the user sees at the top.
 *
 * Slots absent from the published order — filtered out of the sidebar, or
 * created since its last publish — append in store order so cycling still
 * reaches every open session. Stale keys (slot closed since publish) drop out.
 * An empty published order (sidebar never rendered) falls back to store order.
 */
export function orderSlotsBySidebar<T extends { key: string }>(
  slots: readonly T[],
  sidebarOrder: readonly string[] | undefined,
): T[] {
  if (!sidebarOrder?.length) return [...slots]
  const byKey = new Map(slots.map(s => [s.key, s]))
  const ordered: T[] = []
  for (const k of sidebarOrder) {
    const s = byKey.get(k)
    if (s) { ordered.push(s); byKey.delete(k) }
  }
  for (const s of slots) if (byKey.has(s.key)) ordered.push(s)
  return ordered
}

/**
 * True while the chat-jump digit modifier is physically held — Ctrl on Mac in
 * Ctrl+digit mode, Alt otherwise (the exact modifier the Digit1..9 chords
 * use). Drives the sidebar's transient 1–9 row badges, so the user can see
 * which row each digit will pick before committing to one.
 *
 * Cleared on window blur and tab-hide as well as keyup: Alt+Tab and ⌘-Tab
 * steal the keyup, and a stuck badge overlay would otherwise persist until
 * the next keypress.
 */
export function useDigitModifierHeld(): boolean {
  const [held, setHeld] = useState(false)
  useEffect(() => {
    // Identify the modifier STATE via ctrlKey/altKey, and the modifier KEY
    // itself via e.location — modifier keys are the only keys reporting
    // DOM_KEY_LOCATION_LEFT/RIGHT (1/2), so this needs no key-name string
    // literals (which the i18n gate flags). Only a press of the modifier key
    // may set the held state: a modified ordinary key (Alt+ArrowDown) must
    // NOT flip it, or the badge re-render disrupts in-flight row interactions
    // like arrow navigation. Any keyup without the modifier down clears.
    const isHeld = (e: KeyboardEvent) => (getCtrlDigitsEnabled() ? e.ctrlKey : e.altKey)
    const isModifierKey = (e: KeyboardEvent) => e.location === 1 || e.location === 2
    const down = (e: KeyboardEvent) => { if (isHeld(e) && isModifierKey(e)) setHeld(true) }
    const up = (e: KeyboardEvent) => { if (!isHeld(e)) setHeld(false) }
    const clear = () => setHeld(false)
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', clear)
    document.addEventListener('visibilitychange', clear)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', clear)
      document.removeEventListener('visibilitychange', clear)
    }
  }, [])
  return held
}

/**
 * A shortcut as the display surfaces and the legacy handler branches see it:
 * the registry entry's chord for THIS platform, flattened to the field shape the
 * modal, Settings → Shortcuts, the nav hover hint and their tests have always
 * read. `meta` is the ⌘-on-Mac/Ctrl-elsewhere primary (`Chord.mod`); `ctrl` is
 * literal Control. Derived by {@link toShortcutDef} — never hand-written.
 */
export interface ShortcutDef {
  id: string
  key: string
  alt?: boolean
  // Literal Ctrl on EVERY platform (rendered ⌃ on Mac, "Ctrl" elsewhere — see
  // formatShortcut). The chat-jump digits set this on Mac alone so that Mac uses
  // Ctrl instead of Option; a chord that sets it unconditionally (agent monitor)
  // is Ctrl everywhere. For the ⌘-on-Mac/Ctrl-elsewhere shape use `meta`.
  ctrl?: boolean
  meta?: boolean  // Cmd on Mac, Ctrl on Windows/Linux
  shift?: boolean
  /**
   * Already-resolved display label — the EXTENSION SEAM ONLY
   * (`registerPanelShortcut`), where a downstream edition supplies its own string
   * and owns its own localisation. Core entries carry no label: their copy lives in
   * `SHORTCUT_LABEL_KEY` and is resolved per render by `shortcutLabel()`.
   */
  label?: string
  /**
   * Interpolation count for an indexed label — the N in "Jump to chat {{n}}". Set
   * only on the entries whose label carries a number, so the nine chat-jump chords
   * share ONE catalog key instead of nine near-identical ones.
   */
  n?: number
  group: ShortcutGroup
  /** Legacy chords still accepted for one release; rendered muted after the primary. */
  aliases?: readonly Chord[]
  /** Browsers never deliver this chord to the page — see the registry module doc. */
  browserReserved?: boolean
}

/** The registry platform for a `mac` flag. */
export function shortcutPlatform(mac: boolean = IS_MAC): ShortcutPlatform {
  return mac ? 'mac' : 'other'
}

/** A `ShortcutDef` chord shape from a registry chord (or `null` for unbound → no key). */
export function shortcutDefFromChord(base: Pick<ShortcutDef, 'id' | 'group' | 'n' | 'label'>, chord: Chord): ShortcutDef {
  const def: ShortcutDef = { id: base.id, key: chord.key, group: base.group }
  if (chord.mod) def.meta = true
  if (chord.ctrl) def.ctrl = true
  if (chord.alt) def.alt = true
  if (chord.shift) def.shift = true
  if (base.n !== undefined) def.n = base.n
  if (base.label !== undefined) def.label = base.label
  return def
}

/**
 * Flatten one registry entry for `platform`. An entry with no chord on this
 * platform yields `null` and is not listed. Aliases and the browser-reserved
 * flag ride along so the modal can render them.
 */
export function toShortcutDef(entry: ShortcutEntry, platform: ShortcutPlatform = shortcutPlatform()): ShortcutDef | null {
  const chord = entry.defaults[platform]
  if (!chord) return null
  const def = shortcutDefFromChord(entry, chord)
  const aliases = entry.aliases?.[platform]
  if (aliases && aliases.length > 0) def.aliases = aliases
  if (entry.browserReserved) def.browserReserved = true
  return def
}

/**
 * The built-in shortcuts for THIS platform, derived from the registry at module
 * load (IS_MAC is frozen; see its comment). A mutable array on purpose:
 * `registerPanelShortcut` appends a downstream edition's panel chords. The
 * handler below no longer reads chords from here — it asks the registry — so this
 * is the DISPLAY list, and the registry is the source of truth.
 */
export const DEFAULT_SHORTCUTS: ShortcutDef[] = SHORTCUT_REGISTRY
  .map(e => toShortcutDef(e))
  .filter((d): d is ShortcutDef => d !== null)

/**
 * Catalog KEY for each entry's display label, by `ShortcutDef.id`.
 *
 * Keys, not strings, and a separate table rather than a field on the entries: this
 * module is evaluated once at import, so an `i18nT()` call inside `DEFAULT_SHORTCUTS`
 * would freeze the boot language and never re-resolve on a language switch. The
 * lookup happens in `shortcutLabel()`, which runs during render.
 *
 * Flat `Record` of full literal keys, indexed inline at the `i18nT()` call, because
 * that is the form `scripts/check-i18n-keys.mjs` resolves statically — it widens
 * `SHORTCUT_LABEL_KEY[id]` to the whole value set and verifies every member exists.
 * A `labelKey` field on each entry (the `surfaces/registry.ts` shape) would read
 * more naturally but forces `i18nT(def.labelKey)` at the call site, which is
 * unresolvable and would add a second entry to `dynamic-keys-baseline.json` — a
 * ratchet that only moves down.
 *
 * The nine chat-jump ids share ONE key and pass `n`: nine catalog entries differing
 * only by a digit is nine strings for a translator to keep consistent, ten times
 * over. Same for the five remote-crew slots. `instance-1` is the Local tab, which is
 * named rather than numbered, so it keeps its own key.
 */
export const SHORTCUT_LABEL_KEY: Record<string, string> = {
  'chat-1': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-2': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-3': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-4': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-5': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-6': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-7': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-8': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-9': 'hooks.useKeyboardShortcuts.jump_to_chat',
  'chat-letters': 'hooks.useKeyboardShortcuts.jump_to_chat_letters',
  'chat-prev': 'hooks.useKeyboardShortcuts.previous_chat',
  'chat-next': 'hooks.useKeyboardShortcuts.next_chat',
  'chat-prev-bracket': 'hooks.useKeyboardShortcuts.previous_chat',
  'chat-next-bracket': 'hooks.useKeyboardShortcuts.next_chat',
  'chat-mru': 'hooks.useKeyboardShortcuts.last_visited_chat_mru',
  'chat-mru-back': 'hooks.useKeyboardShortcuts.walk_back_mru_history',
  'nav-chat': 'hooks.useKeyboardShortcuts.chats_panel',
  'nav-notifications': 'hooks.useKeyboardShortcuts.notifications_panel',
  'nav-projects': 'hooks.useKeyboardShortcuts.projects_panel',
  'nav-schedule': 'hooks.useKeyboardShortcuts.schedule_panel',
  // Reused: the same commands as the top-bar arrows, so the reference list and
  // the buttons cannot drift apart.
  'history-back': 'app.nav_back',
  'history-forward': 'app.nav_forward',
  'focus-input': 'hooks.useKeyboardShortcuts.focus_text_input',
  'focus-approval': 'hooks.useKeyboardShortcuts.focus_pending_approval',
  // Reused: the same command as the chat sidebar's own New chat / Close session
  // controls, so the reference list and the buttons cannot drift apart.
  'new-chat': 'pages.chatSidebar.new_chat',
  'close-chat': 'pages.chatSidebar.close_session',
  'shortcuts-modal': 'hooks.useKeyboardShortcuts.open_shortcuts_help',
  'open-settings': 'hooks.useKeyboardShortcuts.open_settings',
  'cycle-agent': 'hooks.useKeyboardShortcuts.cycle_agent',
  'cycle-prev-agent': 'hooks.useKeyboardShortcuts.previous_agent',
  'cycle-reasoning': 'hooks.useKeyboardShortcuts.cycle_reasoning_effort',
  'cycle-prev-reasoning': 'hooks.useKeyboardShortcuts.previous_reasoning_effort',
  'cycle-approval': 'hooks.useKeyboardShortcuts.cycle_approval_mode',
  'cycle-prev-approval': 'hooks.useKeyboardShortcuts.previous_approval_mode',
  'cycle-model': 'hooks.useKeyboardShortcuts.cycle_model',
  'cycle-prev-model': 'hooks.useKeyboardShortcuts.previous_model',
  'toggle-focus-mode': 'hooks.useKeyboardShortcuts.toggle_focus_mode',
  // Reused: the ChatInput control this chord fires.
  'optimize-prompt': 'components.chatInput.optimize_prompt',
  'agent-monitor': 'hooks.useKeyboardShortcuts.open_agent_monitor',
  'stop-speaking': 'hooks.useKeyboardShortcuts.stop_speaking',
  'instance-1': 'hooks.useKeyboardShortcuts.switch_to_local',
  'instance-2': 'hooks.useKeyboardShortcuts.switch_to_remote_crew',
  'instance-3': 'hooks.useKeyboardShortcuts.switch_to_remote_crew',
  'instance-4': 'hooks.useKeyboardShortcuts.switch_to_remote_crew',
  'instance-5': 'hooks.useKeyboardShortcuts.switch_to_remote_crew',
  'instance-6': 'hooks.useKeyboardShortcuts.switch_to_remote_crew',
}

/** Catalog KEY for each group's displayed heading. Never the discriminant — see `ShortcutGroup`. */
export const SHORTCUT_GROUP_LABEL_KEY: Record<ShortcutGroup, string> = {
  'chat-navigation': 'hooks.useKeyboardShortcuts.group_chat_navigation',
  'panel-navigation': 'hooks.useKeyboardShortcuts.group_panel_navigation',
  'actions': 'hooks.useKeyboardShortcuts.group_actions',
  'remote-crews': 'hooks.useKeyboardShortcuts.group_remote_crews',
}

/**
 * Localised display label for a shortcut, resolved at RENDER time.
 *
 * An entry with no catalog key is a downstream registration via
 * `registerPanelShortcut`, which supplies its own already-resolved `label`; that
 * string is returned verbatim rather than dressed up, and its id is the last resort
 * so a mis-registered entry is legible as an identifier instead of blank.
 *
 * `hasOwnProperty`, not `in`: ids reach here from the extension seam, so an entry
 * registered as `toString` or `constructor` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next.
 */
export function shortcutLabel(def: ShortcutDef): string {
  if (!Object.prototype.hasOwnProperty.call(SHORTCUT_LABEL_KEY, def.id)) return def.label ?? def.id
  return def.n === undefined
    ? i18nT(SHORTCUT_LABEL_KEY[def.id])
    : i18nT(SHORTCUT_LABEL_KEY[def.id], { n: def.n })
}

/**
 * Localised heading for a shortcut group, resolved at RENDER time. Takes the
 * `string` the display surfaces actually hold; an unknown id (a downstream group
 * this build does not know) is returned verbatim rather than blanking the heading.
 */
export function shortcutGroupLabel(group: string): string {
  return Object.prototype.hasOwnProperty.call(SHORTCUT_GROUP_LABEL_KEY, group)
    ? i18nT(SHORTCUT_GROUP_LABEL_KEY[group as ShortcutGroup])
    : group
}

/**
 * The instance-switch entries, exported as the single source of truth for
 * useInstanceShortcuts: the handler accepts exactly Digit1..Digit<N> where N =
 * INSTANCE_SHORTCUTS.length, so the chords the modal advertises and the chords
 * the handler claims can never drift apart.
 *
 * Compares the stable `ShortcutGroup` ID, never the displayed heading: the heading
 * is localised, so filtering on it would yield an empty list — and a silently
 * unbound ⌘1..⌘6 — in every language but English.
 */
export const INSTANCE_SHORTCUTS = DEFAULT_SHORTCUTS.filter(s => s.group === 'remote-crews')

/**
 * The core Alt+<key> panel-navigation chords. Single source of truth for both
 * the handler dispatch and the extension-seam duplicate guard, so a downstream
 * registration can never shadow a core panel.
 */
export const CORE_PANEL_MAP: Record<string, string> = {
  KeyC: '/chat',
  KeyN: '/notifications',
  KeyP: '/projects',
  KeyS: '/schedule',
}

/**
 * Alt (no-shift) codes the handler consumes BEFORE it reaches panel routing.
 * A downstream panel registered on one of these would be advertised in the
 * shortcuts modal yet never fire (the earlier branch returns first), so they
 * are reserved: the core panel chords, plus the non-shift Alt actions the
 * handler dispatches ahead of the panelMap block (shortcuts modal, settings,
 * focus-input, MRU toggle) and the Alt+digit chat-jumps. Keep in sync with the
 * handler's pre-panel branches below.
 *
 * Exported so `extensionSeams.test.tsx` can guard the sync: a drift test parses
 * this module's handler for the codes it consumes before the panelMap block and
 * asserts each is reserved here, so a new pre-panel chord added without updating
 * this set fails CI rather than silently shadowing a downstream panel.
 */
export const RESERVED_PANEL_CODES: ReadonlySet<string> = new Set<string>([
  ...Object.keys(CORE_PANEL_MAP),
  'KeyK', // shortcuts modal (Alt+K)
  'Comma', // settings (Cmd+, on macOS, Alt+, elsewhere; Alt+, stays bound on Mac)
  'Enter', // focus text input (Alt+Enter)
  'Backquote', // MRU toggle (Alt+`)
  'ArrowLeft',
  'ArrowRight', // prev/next chat
  'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5',
  'Digit6', 'Digit7', 'Digit8', 'Digit9', // chat jump
])

/**
 * Letters a jump chord must avoid for a reason OTHER than pre-panel routing, so
 * they are absent from RESERVED_PANEL_CODES and cannot be derived from it: global
 * Ctrl/⌘ chords that reach hasCommandModifier via plain Ctrl on macOS and would
 * double-fire with the Mac Ctrl-jump branch. Each is an independent document
 * listener, so an unexcluded letter fires both actions at once; excluded
 * uniformly across platforms so a session shows the same letter on every OS.
 *  - a — select-all in a focusable list (Ctrl/⌘+A)
 *  - d — split the focused pane (Ctrl/⌘+D)
 *  - f — message search / Markdown find (Ctrl/⌘+F)
 *  - g — agent monitor (Ctrl+G, literal Ctrl on every platform)
 *  - t/w — file-explorer new-folder-tab / close-tab (Ctrl/⌘+T, Ctrl/⌘+W)
 */
const JUMP_LETTER_GLOBAL_EXCLUDE = new Set(['a', 'd', 'f', 'g', 't', 'w'])

/**
 * Lowercase letters a pre-panel chord owns, read straight from
 * RESERVED_PANEL_CODES so the two never drift: a panel or pre-panel Alt+<letter>
 * chord added to that set — which the extension-seams drift test already forces —
 * drops its jump letter in the same edit instead of being silently shadowed by
 * one. Only single-letter Key* codes name a jump letter; the Comma, Enter,
 * Backquote, Arrow, and Digit codes are not letters and contribute nothing here.
 */
function reservedPanelLetters(): Set<string> {
  const out = new Set<string>()
  for (const code of RESERVED_PANEL_CODES) {
    const m = /^Key([A-Z])$/.exec(code)
    if (m) out.add(m[1].toLowerCase())
  }
  return out
}

/**
 * Letters usable for chat-jumps 10+ (digit 1–9 stay digits; the 10th session
 * onward gets a letter). a–z minus every letter another unshifted chord owns —
 * the pre-panel chords (`reservedPanelLetters`), the global editor chords
 * (`JUMP_LETTER_GLOBAL_EXCLUDE`), and any code a downstream edition registered
 * via registerPanelShortcut (`EXTRA_PANEL_ROUTES`, resolved at module init before
 * any keypress or badge render, panels winning over jump letters). Computed per
 * call, not a constant, so the runtime exclusions are honored.
 */
export function jumpLetters(): string[] {
  const panelLetters = reservedPanelLetters()
  const out: string[] = []
  for (let i = 0; i < 26; i++) {
    const ch = String.fromCharCode(97 + i)
    if (panelLetters.has(ch)) continue
    if (JUMP_LETTER_GLOBAL_EXCLUDE.has(ch)) continue
    if (('Key' + ch.toUpperCase()) in EXTRA_PANEL_ROUTES) continue
    out.push(ch)
  }
  return out
}

/** Jump target index (0-based) for a key code: Digit1–9 → 0–8, letters → 9+.
 *  -1 when the code is not a jump chord (excluded letter, other key). */
export function jumpIndexForCode(code: string): number {
  if (code >= 'Digit1' && code <= 'Digit9') return parseInt(code.charAt(5)) - 1
  if (code.startsWith('Key')) {
    const li = jumpLetters().indexOf(code.slice(3).toLowerCase())
    return li >= 0 ? 9 + li : -1
  }
  return -1
}

/** Badge label for the Nth (0-based) jump target: '1'–'9' then the letter
 *  sequence; null past the addressable range. */
export function jumpLabelFor(index: number): string | null {
  if (index < 0) return null
  if (index < 9) return String(index + 1)
  return jumpLetters()[index - 9] ?? null
}

/** Map a KeyboardEvent.code to the display key the shortcuts modal shows. */
function _displayKeyForCode(code: string): string {
  if (code.startsWith('Key')) return code.slice(3).toLowerCase()
  if (code.startsWith('Digit')) return code.slice(5)
  return code
}

/**
 * Panel-navigation extension seam. A downstream edition that adds a navigable
 * panel registers its Alt+<key> chord here (from the extensions.ts composition
 * root, at module-load time) instead of editing this file's panel map +
 * `DEFAULT_SHORTCUTS` on every upstream sync. Registering advertises the chord
 * in the shortcuts modal AND makes the handler navigate to it. The core
 * registers none.
 *
 * The chord is identified solely by KeyboardEvent.code; the displayed key is
 * DERIVED from it (`_displayKeyForCode`) so the advertised chord can never
 * diverge from the handled one. A registration whose code collides with a core
 * panel chord, an already-registered extension, OR any Alt chord the handler
 * consumes before panel routing (`RESERVED_PANEL_CODES` — otherwise the panel
 * would be unreachable) routes through `reportSeamCollision`: fail-loud in
 * dev/test, warn-and-ignore in production (core/first wins).
 */
const EXTRA_PANEL_ROUTES: Record<string, string> = {}

export function registerPanelShortcut(entry: { code: string; path: string; label: string }): void {
  if (RESERVED_PANEL_CODES.has(entry.code) || entry.code in EXTRA_PANEL_ROUTES) {
    reportSeamCollision(
      'shortcuts',
      `panel shortcut ${entry.code} is reserved or already registered; ignoring`,
    )
    return
  }
  EXTRA_PANEL_ROUTES[entry.code] = entry.path
  DEFAULT_SHORTCUTS.push({
    id: `nav-${entry.path.replace(/^\//, '')}`,
    key: _displayKeyForCode(entry.code),
    alt: true,
    // The downstream edition owns this string and its localisation: there is no
    // catalog key for a panel the core does not know about, so `shortcutLabel()`
    // returns it verbatim.
    label: entry.label,
    group: 'panel-navigation',
  })
}

// (Mac detection: use isMacPlatform() — the file's single live detector.)

/**
 * True when `e` is the platform's "open Settings" chord, as the registry defines
 * it: ⌘, on macOS / Ctrl+, on Windows-Linux, plus the Option/Alt+, alias.
 *
 * ⌘, is the OS-standard Preferences chord on macOS, and the one the desktop app's
 * "Settings…" menu item advertises (electron/app-menu.js binds `CmdOrCtrl+,`);
 * Ctrl+, is the VS Code convention on Windows/Linux. In the desktop shell the
 * menu accelerator fires first there, which is fine — same destination.
 *
 * Option/Alt+, stays accepted everywhere, rendered as an alias: a Mac browser can
 * claim ⌘, as its own Preferences accelerator before the page ever sees the
 * keydown, so dropping the Option chord would leave those users with no keyboard
 * route to Settings. Exactly one primary modifier either way, so the chord can't
 * fire from ⌘⌥, or ⌃, misses.
 *
 * Reads the FACTORY defaults, not user overrides: the caller that needs overrides
 * (the keydown handler) goes through `matchShortcutEvent`. `mac` is injectable so
 * both platform behaviours are testable without reloading the module (IS_MAC is
 * fixed at module load).
 */
export function isSettingsChord(
  e: Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  mac: boolean = IS_MAC,
): boolean {
  if (e.code !== 'Comma') return false
  const entry = shortcutEntry('open-settings')
  if (!entry) return false
  const platform = shortcutPlatform(mac)
  const ev = { ...e, key: ',' }
  const primary = entry.defaults[platform]
  if (primary && chordMatchesEvent(ev, primary, platform)) return true
  return (entry.aliases?.[platform] ?? []).some(a => chordMatchesEvent(ev, a, platform))
}

/**
 * Session-cycle chord: ⌘[ / ⌘] on macOS, Ctrl+[ / Ctrl+] on Windows-Linux —
 * step one session backwards/forwards through the sidebar order.
 *
 * Keyed by KeyboardEvent.code, so the chord is POSITIONAL: on layouts where the
 * bracket glyphs sit elsewhere (or need AltGr to type) the physical keys in the
 * US-QWERTY bracket positions still work, matching how every other chord in
 * this module is matched.
 */
const SESSION_STEP_BY_CODE: Record<string, number> = { BracketLeft: -1, BracketRight: 1 }

/**
 * The step this event asks for (-1 back, +1 forward), or 0 when it is not the
 * session-cycle chord. Exactly ONE primary modifier and no Alt/Shift, so it
 * cannot fire from ⌘⌥[ misses and cannot shadow Alt+arrow chat-nav, the Mac
 * Ctrl+digit chat-jumps, or ⌘/Ctrl+digit remote-crew switching.
 *
 * `mac` is injectable for the same reason as isSettingsChord: IS_MAC is fixed
 * at module load, so both platform behaviours would otherwise be untestable.
 */
export function sessionCycleStep(
  e: Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  mac: boolean = IS_MAC,
): number {
  const primary = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey
  if (!primary || e.altKey || e.shiftKey) return 0
  return SESSION_STEP_BY_CODE[e.code] ?? 0
}

/**
 * True when `e` is the agent-monitor chord: literal Ctrl+G on EVERY platform.
 *
 * Deliberately NOT the usual ⌘-on-Mac substitution the other primary-modifier
 * chords use. The kiro-cli backend prints "Press ctrl+g to monitor progress."
 * into its crew-pipeline tool result, and that string lives inside the backend
 * binary — we cannot re-word it per OS. So the chord the user is TOLD to press
 * has to be the chord that actually fires, on every platform. On macOS
 * find-next is ⌘G, leaving ⌃G free.
 *
 * Keyed by KeyboardEvent.code, so the chord is POSITIONAL like every other in
 * this module. Exactly one primary modifier and no Alt/Shift, so it cannot fire
 * from ⌃⌘G / ⌃⌥G misses, and cannot shadow the Mac Ctrl+digit chat-jumps.
 *
 * Requiring `ctrlKey && !altKey` is also why 'KeyG' is NOT added to
 * RESERVED_PANEL_CODES: this branch is unreachable for an Alt+G keystroke, so it
 * cannot shadow a downstream Alt+G panel registration and reserving the code
 * would over-claim the extension seam.
 */
export function isAgentMonitorChord(
  e: Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
): boolean {
  if (e.code !== 'KeyG') return false
  return e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey
}

/**
 * Neighbour of `curIdx` in a list of `len`, stepping by `step` and wrapping at
 * both ends. Returns -1 for an empty list. With no current selection (-1) a
 * backward step lands on the last entry and a forward step on the first —
 * the behaviour both the Alt+arrow and the bracket chord want.
 */
export function wrapIndex(len: number, curIdx: number, step: number): number {
  if (len === 0) return -1
  if (curIdx < 0) return step < 0 ? len - 1 : 0
  return (curIdx + step + len) % len
}

/**
 * True when the keystroke came from inside an embedded terminal. Ctrl+[ is a
 * real PTY keystroke there (it sends ESC — how vim users leave insert mode), so
 * the session-cycle chord must let it through rather than swallow it.
 */
function isTerminalTarget(target: EventTarget | null): boolean {
  const el = target as Element | null
  return !!el && typeof el.closest === 'function' && !!el.closest('.xterm')
}

/** The layout condition that hides the top-bar history arrows, read LIVE at
 *  keypress so the handler's document listener never re-registers on resize.
 *  Same breakpoint as `useIsMobile` — one source of truth for "narrow". */
function isNarrowViewport(): boolean {
  return typeof window.matchMedia === 'function'
    && window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`).matches
}

export function formatShortcut(def: ShortcutDef): string {
  const mac = isMacPlatform()
  const parts: string[] = []
  if (def.meta) parts.push(mac ? '\u2318' : 'Ctrl')
  if (def.ctrl) parts.push(mac ? '\u2303' : 'Ctrl')
  if (def.alt) parts.push(mac ? '\u2325' : 'Alt')
  if (def.shift) parts.push(mac ? '\u21e7' : 'Shift')
  const keyLabel = def.key === 'ArrowLeft' ? '\u2190' : def.key === 'ArrowRight' ? '\u2192' : def.key === '`' ? '`' : def.key === 'Enter' ? (mac ? '\u23ce' : 'Enter') : def.key === ',' ? ',' : def.key === 'Escape' ? '\u238b' : def.key.toUpperCase()
  parts.push(keyLabel)
  return parts.join(mac ? '' : ' + ')
}

/**
 * Display caps for a registry chord — {@link formatShortcut} for a `Chord`, so
 * an alias renders with exactly the glyphs its primary does. Split on the same
 * separator the surfaces already split `formatShortcut` output on.
 */
export function formatChordCaps(chord: Chord, id = ''): string[] {
  return formatShortcut(shortcutDefFromChord({ id, group: 'actions' }, chord)).split(' + ')
}

/**
 * The live display entry for a registry id: the user's override when one is
 * stored, else the platform default with its aliases. `null` when the id is
 * unknown or currently unbound. Reads storage once per call — for a reactive
 * surface use {@link useShortcutBindings}.
 */
export function resolveShortcutDef(id: string, overrides: ShortcutOverrides = loadShortcutOverrides()): ShortcutDef | null {
  const entry = shortcutEntry(id)
  if (!entry) return null
  const r = resolveShortcuts(overrides, shortcutPlatform())[id]
  if (!r?.primary) return null
  const def = shortcutDefFromChord(entry, r.primary)
  if (r.aliases.length > 0) def.aliases = r.aliases
  if (entry.browserReserved) def.browserReserved = true
  return def
}

/**
 * The resolved bindings for every registry id, re-read when Settings (P3) or
 * another tab writes an override. The keydown handler and the display surfaces
 * both hold this shape so they cannot disagree about what a chord does.
 */
export function useShortcutBindings(): Record<string, ResolvedShortcut> {
  const [overrides, setOverrides] = useState<ShortcutOverrides>(() => loadShortcutOverrides())
  useEffect(() => {
    const refresh = () => setOverrides(loadShortcutOverrides())
    const onStorage = (e: StorageEvent) => { if (e.key === SHORTCUT_OVERRIDES_KEY) refresh() }
    window.addEventListener(SHORTCUT_OVERRIDES_EVENT, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(SHORTCUT_OVERRIDES_EVENT, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return useMemo(() => resolveShortcuts(overrides, shortcutPlatform()), [overrides])
}

interface UseKeyboardShortcutsOpts {
  onToggleShortcutsModal: () => void
  onNewChat: () => void
  onCycleAgent?: () => void
  onCyclePrevAgent?: () => void
  onCycleReasoningEffort?: () => void
  onCyclePrevReasoningEffort?: () => void
  onCycleApprovalMode?: () => void
  onCyclePrevApprovalMode?: () => void
  onCycleModel?: () => void
  onCyclePrevModel?: () => void
  onToggleFocusMode?: () => void
  onToggleLeftSidebar?: () => void
  onToggleSessionPanel?: () => void
  onToggleSidePanel?: () => void
  /**
   * Toggle the docked terminal panel. Owned by App rather than dispatched here:
   * the panel's store is module-level, but the DECISION needs App-only state (the
   * `dashboard.terminal.enabled` probe, whether the panel is popped out into its
   * own window, and the active session's project as the shell's cwd). Left
   * undefined when the terminal is disabled, so the chord no-ops exactly as the
   * nav row disappears.
   */
  onToggleTerminal?: () => void
  disabled?: boolean
}

export function useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, onCycleAgent, onCyclePrevAgent, onCycleReasoningEffort, onCyclePrevReasoningEffort, onCycleApprovalMode, onCyclePrevApprovalMode, onCycleModel, onCyclePrevModel, onToggleFocusMode, onToggleLeftSidebar, onToggleSessionPanel, onToggleSidePanel, onToggleTerminal, disabled }: UseKeyboardShortcutsOpts) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const guardedHistoryStep = useGuardedHistoryStep()
  const appStore = useAppStore()
  const mruIndexRef = useRef(-1)
  // Set true right after a char-producing Alt shortcut (Alt+`) fires inside a
  // text field. On macOS those combos are dead keys (Option+` = grave accent),
  // and keydown.preventDefault() cannot cancel the composed character — it
  // arrives via beforeinput. The guard below eats it.
  const suppressNextInputRef = useRef(false)
  const [enabled, setEnabled] = useState(() => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
  const [ctrlDigits, setCtrlDigits] = useState(() => getCtrlDigitsEnabled())
  // In state, not read per keystroke, to keep the hot keydown path off localStorage.
  const [panelBindings, setPanelBindings] = useState<PanelToggleOverrides>(() => loadPanelToggleOverrides())
  // The registry's resolved chords (defaults + user overrides), same reasoning.
  const bindings = useShortcutBindings()

  // Listen for toggle changes from Settings
  useEffect(() => {
    const onToggle = () => {
      setEnabled(localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
      setCtrlDigits(getCtrlDigitsEnabled())
    }
    window.addEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
    return () => window.removeEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
  }, [])

  // Pick up rebinds from Settings (same-tab event) and other tabs (storage).
  useEffect(() => {
    const refresh = () => setPanelBindings(loadPanelToggleOverrides())
    const onStorage = (e: StorageEvent) => { if (e.key === PANEL_TOGGLE_SHORTCUTS_KEY) refresh() }
    window.addEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  // Reset MRU walk index when Alt is released
  useEffect(() => {
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Alt') mruIndexRef.current = -1
    }
    document.addEventListener('keyup', onKeyUp)
    return () => document.removeEventListener('keyup', onKeyUp)
  }, [])

  // Cancel the stray character a macOS dead-key Alt shortcut would otherwise
  // insert (e.g. Alt+` switching the slot AND typing a backtick). Capture phase
  // so it runs before the focused field handles the input. No-op on
  // Linux/Windows where keydown.preventDefault() already suppresses it.
  useEffect(() => {
    const onBeforeInput = (e: Event) => {
      if (suppressNextInputRef.current) {
        suppressNextInputRef.current = false
        e.preventDefault()
      }
    }
    document.addEventListener('beforeinput', onBeforeInput, true)
    return () => document.removeEventListener('beforeinput', onBeforeInput, true)
  }, [])

  /**
   * Panel-toggle id → the action that toggles it, or `undefined` when the host
   * passed none.
   *
   * `undefined` means NOT BOUND, and both dispatch phases must treat it that way:
   * a chord with no action behind it is left for the browser, never claimed and
   * then dropped. `onToggleTerminal` is the case that exists today — App leaves it
   * undefined while `dashboard.terminal.enabled` is false — and swallowing the key
   * there would suppress the browser's native Ctrl+J (Downloads) in exchange for
   * nothing.
   *
   * Keying by id also keeps the two phases in step: the capture-phase skip-shell
   * listener dispatches through this same map, so extending
   * {@link PANEL_TOGGLES_SKIPPING_SHELL} needs no second per-panel branch.
   */
  const panelToggleActions = useMemo<Record<PanelToggleId, (() => void) | undefined>>(() => ({
    'left-sidebar': onToggleLeftSidebar,
    'session-panel': onToggleSessionPanel,
    'side-panel': onToggleSidePanel,
    'terminal': onToggleTerminal,
  }), [onToggleLeftSidebar, onToggleSessionPanel, onToggleSidePanel, onToggleTerminal])

  const handler = useCallback((e: KeyboardEvent) => {
    const tag = (e.target as HTMLElement)?.tagName
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable
    // Read at keypress time; subscribing re-renders the root on every slots frame.
    const { dashboard: { slots, sidebarOrder }, chat: { activeSlot, slotHistory } } = appStore.getState()
    // Jump/cycle targets in the order the sidebar displays them, so Ctrl/Alt+N
    // picks the Nth visible row and cycling walks the list the user is looking
    // at. `sidebarOrder` may be undefined in stores seeded before the field
    // existed (tests with partial preloaded state).
    const orderedSlots = orderSlotsBySidebar(slots, sidebarOrder)

    // Every KEYBOARD-driven session switch goes through here. On macOS it
    // first releases the composer (blur + skip the switch's one autofocus):
    // letter chords are input-gated there (Cocoa readline / Option
    // composition), so leaving focus in the composer after a keyboard switch
    // made the NEXT chord dead until the user clicked the chat. Keyboard
    // navigation stays chainable; `/` or a click focuses the composer to
    // type. Pointer-driven switches (sidebar row clicks) never come through
    // here and keep their type-immediately autofocus. Live isMacPlatform()
    // call, not IS_MAC, so tests can exercise the Mac side via setPlatform.
    const kbSwitch = (key: string) => {
      // Release only on a REAL target change: a same-key switch (self-jump,
      // or a single-session bracket/arrow wrap) produces no autoFocusKey
      // transition, so ChatInput's effect would never consume the one-shot —
      // the leaked flag would blur the composer with no refocus AND silently
      // eat the NEXT pointer-driven switch's autofocus.
      if (isMacPlatform() && key !== activeSlot) releaseComposerForKeyboardSwitch()
      dispatch(switchSlot(key))
      navigate('/chat')
    }

    // On Mac (when Ctrl+digit mode enabled), Ctrl+digit switches chats —
    // Ctrl+letter reaches sessions 10+ (see jumpLetters for the exclusions;
    // Ctrl+G stays the agent monitor). Letters are input-gated: Ctrl+A/E/K
    // etc. are readline bindings inside text fields on macOS, so a letter
    // jump never fires while typing. Digits keep firing in inputs (no
    // text-editing meaning, and that has been the behavior since #4727).
    // Check for that first, before the Alt-based gate.
    const code = e.code
    // Which registry shortcut this keystroke is, if any — primary or alias, with
    // the user's overrides applied. `code` FAMILIES (digit/letter jumps, MRU walk,
    // Alt+arrows, panel nav, the bracket cycle, the instance switcher) never match
    // here; their branches below own them. A ⌘/Ctrl chord aimed at an embedded
    // terminal is the PTY's (Ctrl+W is kill-word, Ctrl+N next-history), so it is
    // not ours to claim — the same rule the bracket and agent-monitor chords apply.
    // Likewise a ⌘/Ctrl chord a focused editor already claimed (`defaultPrevented`
    // — CodeMirror's Ctrl+/ toggle-comment, the Pierre editor's ⌘S) stays with the
    // editor, as the panel toggles already defer. The Option/Alt aliases keep their
    // shipped behaviour and are not subject to either rule.
    let hit = matchShortcutEvent(e, bindings, shortcutPlatform())
    if (hit && (e.ctrlKey || e.metaKey) && (e.defaultPrevented || isTerminalTarget(e.target))) hit = null
    // The history chords are UN-CLAIMED (nulled, falling through) for TEXT
    // FIELDS only: there ⌘←/→ is caret line-start/line-end on macOS and
    // Ctrl+←/→ is word-jump elsewhere, the field consumes the keystroke, and
    // no browser navigation happens. The narrow-viewport gate is NOT an
    // unclaim — see the map entries: on macOS an unclaimed ⌘← is the
    // browser's own Back, which would pop PAST the draft guard whenever its
    // trap is unarmed (post-reload) and unmount a dirty editor. Narrow
    // viewports claim the chord and do nothing.
    if ((hit === 'history-back' || hit === 'history-forward') && isInput) hit = null

    if (ctrlDigits && e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
      const jumpIdx = jumpIndexForCode(code)
      // A LETTER is claimed only when it currently maps to a session
      // (jumpIdx < orderedSlots.length): browsers own Alt/Ctrl+letter chords
      // of their own (Ctrl+E address bar, etc.), so an unmapped letter must
      // fall through to the browser instead of being swallowed as a no-op.
      // Digits keep their pre-existing always-claim behavior — they collide
      // with nothing and swallowing a dead digit avoids surprise typing.
      const mapped = jumpIdx >= 0 && jumpIdx < orderedSlots.length
      if (jumpIdx >= 0 && (jumpIdx < 9 || (!isInput && mapped))) {
        if (!enabled || disabled) return
        e.preventDefault()
        if (mapped) kbSwitch(orderedSlots[jumpIdx].key)
        return
      }
    }

    // Settings — ⌘, on macOS, Ctrl+, on Windows/Linux, Option/Alt+, alias
    // everywhere (see the registry entry for why). Handled BEFORE the Alt gate
    // below because the primary chord carries no Alt. Fires even when shortcuts
    // are globally disabled, so the user can always reach the toggle that
    // re-enables them.
    if (hit === 'open-settings') {
      e.preventDefault()
      navigate('/settings')
      return
    }


    // ⌘[ / ⌘] on macOS, Ctrl+[ / Ctrl+] on Windows-Linux: step to the
    // previous/next session in sidebar order, wrapping at both ends — the same
    // move as Alt+←/→, on a chord that survives being inside the composer
    // (unlike Alt+arrow, which stays out of text fields to preserve word-jump;
    // ⌘/Ctrl+bracket has no text-editing meaning). Handled BEFORE the Alt gate
    // because the chord carries no Alt. Skipped when the keystroke came from a
    // terminal, where Ctrl+[ is ESC and belongs to the PTY.
    const step = sessionCycleStep(e)
    if (step !== 0 && !isTerminalTarget(e.target)) {
      if (!enabled || disabled) return
      // Claim the keystroke: on macOS ⌘[ / ⌘] are the browser's Back/Forward.
      e.preventDefault()
      const nextIdx = wrapIndex(orderedSlots.length, activeSlot ? orderedSlots.findIndex(s => s.key === activeSlot) : -1, step)
      if (nextIdx >= 0) kbSwitch(orderedSlots[nextIdx].key)
      return
    }

    // Ctrl+G: open the agent monitor — the Subagents activity tab ("Live agent
    // activity & transcripts"). This is the chord the kiro-cli backend advertises
    // in its crew-pipeline tool result ("Press ctrl+g to monitor progress").
    // Handled BEFORE the Alt gate because the chord carries no Alt.
    //
    // Deliberately fires INSIDE text fields: the hint is read while a crew runs
    // and focus is normally in the composer, so an isInput bail-out would make it
    // dead exactly when it is needed. Ctrl+G has no text-editing meaning there.
    // Skipped for terminal targets, where Ctrl+G is BEL and belongs to the PTY.
    //
    // Routes to /chat as well as opening the tab, because the activity panel is
    // owned by the chat page — same reasoning as the bracket chords above.
    if (isAgentMonitorChord(e) && !isTerminalTarget(e.target)) {
      if (!enabled || disabled) return
      e.preventDefault()
      dispatch(openActivityToTab('subagents'))
      navigate('/chat')
      return
    }

    // User-rebindable toggles for the sidebar / session list / activity panel.
    // Before the Alt gate because the defaults are ⌘/Ctrl chords with no Alt;
    // like the bracket chords they fire inside the composer but yield to a
    // terminal. `defaultPrevented` defers to a handler that already claimed the
    // key (e.g. the Pierre editor's capture-phase ⌘S save), so a shared chord
    // saves there rather than also toggling a panel.
    //
    // A panel in `PANEL_TOGGLES_SKIPPING_SHELL` keeps its chord even while a
    // terminal holds focus — see that set for why the terminal toggle must.
    //
    // A panel whose host passed no callback is NOT BOUND, so its chord must fall
    // through untouched rather than be claimed into a no-op: the terminal toggle
    // is undefined while `dashboard.terminal.enabled` is false, and claiming it
    // there would `preventDefault()` the browser's own Ctrl+J (Downloads) for a
    // feature the user turned off.
    const panelToggle = matchPanelToggleEvent(e, panelBindings)
    const panelAction = panelToggle ? panelToggleActions[panelToggle] : undefined
    if (panelToggle && panelAction && !e.defaultPrevented
        && (PANEL_TOGGLES_SKIPPING_SHELL.has(panelToggle) || !isTerminalTarget(e.target))) {
      if (!enabled || disabled) return
      e.preventDefault()
      panelAction()
      return
    }

    // Shortcuts reference (⌘/ / Ctrl+/, alias Option/Alt+K) — always works, even
    // when disabled or in an input, so the user can always reach the toggle that
    // re-enables shortcuts, and can close the modal with the chord that opened it.
    if (hit === 'shortcuts-modal') {
      e.preventDefault()
      onToggleShortcutsModal()
      return
    }

    // Registry-dispatched actions — the ⌘/Ctrl chords and their Option/Alt
    // aliases. All of them fire INSIDE text fields on purpose: they are chords
    // you reach for mid-sentence in the composer (cycle the model, hide the
    // chrome, start a fresh session), none has a text-editing meaning, and an
    // isInput bail-out would make each one dead exactly where it is wanted. All
    // are gated by the global enable and by `disabled` (the modal is open).
    if (hit !== null) {
      if (!enabled || disabled) return
      const actions: Record<string, () => void> = {
        // Route-history Back/Forward — the keyboard twin of the top-bar arrows
        // (#8258), through the same guarded step so NavigationBackGuard's draft
        // trap applies to both identically. Claimed even with nowhere to go,
        // and claimed on a NARROW viewport as a deliberate no-op: on macOS
        // browsers ⌘←/⌘→ is ALSO native history back/forward, so an unclaimed
        // press would pop past the draft guard whenever its trap is unarmed
        // (post-reload) and unmount a dirty editor — the arrows are hidden
        // there (drill-ins navigate by component state), and a keystroke that
        // walked a stack the visible UI does not reflect is the same wrong in
        // native form. Only the text-field gate unclaims (see the hit-null
        // above): the field consumes the caret chord and never navigates.
        'history-back': () => { if (!isNarrowViewport() && canGoBack()) guardedHistoryStep(-1) },
        'history-forward': () => { if (!isNarrowViewport() && canGoForward()) guardedHistoryStep(1) },
        'cycle-agent': () => onCycleAgent?.(),
        'cycle-prev-agent': () => onCyclePrevAgent?.(),
        'cycle-reasoning': () => onCycleReasoningEffort?.(),
        'cycle-prev-reasoning': () => onCyclePrevReasoningEffort?.(),
        'cycle-approval': () => onCycleApprovalMode?.(),
        'cycle-prev-approval': () => onCyclePrevApprovalMode?.(),
        'cycle-model': () => onCycleModel?.(),
        'cycle-prev-model': () => onCyclePrevModel?.(),
        'toggle-focus-mode': () => onToggleFocusMode?.(),
        // Focus the pending tool-approval row. Firing while typing is safe HERE
        // ONLY BECAUSE IT MOVES FOCUS AND NOTHING ELSE — the decision still costs
        // a second, deliberate press on a control the user can now see is
        // focused. A chord that ANSWERED the prompt would be a one-keystroke path
        // to running a tool call nobody read, which is why this one stops at
        // focus; see queryPendingApprovalAction. Claimed even with no approval
        // pending: the chord collides with no browser binding, so swallowing it
        // avoids a stray newline in the draft; nothing is focused in that case.
        'focus-approval': () => queryPendingApprovalAction()?.focus(),
        // Focus text input — works even from other inputs. Unguarded on purpose:
        // a pressed keyboard shortcut proves a keyboard exists —
        // `focusComposer`'s touch-device skip would wrongly no-op it. Still
        // synchronous whenever the composer is on screen: the resolver runs its
        // callback inline in that case. It defers a single frame only when the
        // composer was COLLAPSED and had to be asked back — without that, "focus
        // text input" silently did nothing for as long as the user left it
        // collapsed, which outlives a reload.
        'focus-input': () => queryComposerOrExpand(ta => ta.focus()),
        // ⌘N / Ctrl+N (alias Option/Alt+Shift+N): new session.
        'new-chat': () => onNewChat(),
        // ⌘W / Ctrl+W (alias Option/Alt+Shift+W): close the current session —
        // same semantics as the header-menu close (gated by confirmCloseSession,
        // dispatches deleteSlot). One addition for the NEW chord surface: a
        // session that is not IDLE always confirms. ⌘W/Ctrl+W is the most
        // habitual chord there is (it closed the WINDOW in the previous desktop
        // release on Windows/Linux), and `confirmCloseSession` defaults off — a
        // default calibrated for the hard-to-mispress ⌥⇧W. An idle session is
        // losslessly reopenable from the sidebar's older-sessions list, so it
        // keeps the user's confirm setting; anything else is where a stray
        // keystroke costs work, so it asks.
        //
        // "Not idle" is the sidebar's own lane inference, not `slot.running`: that
        // flag covers only the slot's own turn and reads FALSE between the cycles
        // of an armed goal loop, during a dynamic workflow, and while background
        // sub-agents run — all of which `deleteSlot` retires. Reusing `inferLane`
        // with the same extras the sidebar computes keeps this gate and the
        // Working/Waiting/Needs-approval lanes from ever disagreeing.
        'close-chat': () => {
          if (!activeSlot) return
          const slot = slots.find(s => s.key === activeSlot)
          const state = appStore.getState()
          const subagentsRunning = selectSidebarSubagentCounts(state)[activeSlot] || 0
          const lane = slot ? inferLane(slot, {
            subagentAwaiting: Math.min(selectSidebarApprovalCounts(state)[activeSlot] || 0, subagentsRunning),
            workflowActive: normalizeRunSessionKey(activeSlot) in selectSidebarWorkflowActive(state),
            goalLoopActive: selectSidebarAutomationRunningKeys(state).includes(activeSlot),
            detailedSubagentsRunning: subagentsRunning > 0,
          }) : 'idle'
          const modChord = e.metaKey || e.ctrlKey
          const mustConfirm = loadChatConfig().confirmCloseSession || (modChord && lane !== 'idle')
          if (!mustConfirm || confirm(i18nT('hooks.useKeyboardShortcuts.close_this_session'))) {
            dispatch(deleteSlot(activeSlot))
          }
        },
      }
      // Every `registry` entry has an action here (shortcutRegistry.test pins the
      // two sets). An id without one is left unclaimed rather than swallowed.
      const action = Object.prototype.hasOwnProperty.call(actions, hit) ? actions[hit] : undefined
      if (action) {
        e.preventDefault()
        action()
        return
      }
    }

    // The code-driven families below all use Alt (Option on Mac)
    if (!e.altKey || e.ctrlKey || e.metaKey) return

    // Suppress all shortcuts when globally disabled via settings
    if (!enabled) return

    // Suppress all other shortcuts when disabled (e.g. modal open)
    if (disabled) return

    // Alt+Shift+`: Walk back MRU history
    if (e.shiftKey && code === 'Backquote') {
      e.preventDefault()
      suppressNextInputRef.current = true
      setTimeout(() => { suppressNextInputRef.current = false }, 0)
      if (slotHistory.length === 0) return
      mruIndexRef.current = Math.min(mruIndexRef.current + 1, slotHistory.length - 1)
      const target = slotHistory[slotHistory.length - 1 - mruIndexRef.current]
      if (target) kbSwitch(target)
      return
    }

    // Alt+`: MRU toggle (last visited)
    if (code === 'Backquote' && !e.shiftKey) {
      e.preventDefault()
      suppressNextInputRef.current = true
      setTimeout(() => { suppressNextInputRef.current = false }, 0)
      const prev = slotHistory.length > 0 ? slotHistory[slotHistory.length - 1] : null
      if (prev && prev !== activeSlot) kbSwitch(prev)
      return
    }

    // Alt+1-9 / Alt+letter: Jump to chat N (when NOT in Ctrl+digit mode).
    // Letters cover sessions 10+; an excluded letter (c/g/k/n/p/s or a
    // downstream-registered panel code) returns -1 here and falls through to
    // its owning branch — panels always win over jump letters. On Windows and
    // Linux, letters fire even while focus is in a text field, exactly like
    // digits: switching sessions autofocuses the composer, so an input gate
    // here killed every CHAINED jump (jump → composer steals focus → next
    // letter dead), and Alt+letter types no character on those platforms. On
    // macOS (this branch = legacy Option mode) letters stay input-gated:
    // Option+letter COMPOSES characters (Option+A = å, dead-key accents), so
    // a mapped letter firing mid-typing would eat the typed character and
    // yank the session — same protection the Ctrl branch keeps for readline.
    if (!ctrlDigits && !e.shiftKey) {
      const jumpIdx = jumpIndexForCode(code)
      // Same letter claim rule as the Mac Ctrl branch: a letter is claimed
      // only when it maps to a session, so unmapped letters (Alt+D address
      // bar, Alt+F/E browser menus on Windows/Linux) fall through to the
      // browser instead of being swallowed as dead no-ops.
      const mapped = jumpIdx >= 0 && jumpIdx < orderedSlots.length
      // Terminals are excluded even on non-Mac: Alt+B/F are readline word
      // motions on the shell command line (xterm's helper textarea satisfied
      // the old isInput gate, so this exclusion preserves, not adds, the
      // shipped terminal behavior). The composer chained-jump fix above is
      // about ordinary text fields, never the PTY.
      if (jumpIdx >= 0 && (jumpIdx < 9 || (mapped && (!isInput || !IS_MAC) && !isTerminalTarget(e.target)))) {
        e.preventDefault()
        if (mapped) kbSwitch(orderedSlots[jumpIdx].key)
        return
      }
    }

    // Alt+←/→: Previous/next chat (skip when in text input to preserve word-jump)
    if ((code === 'ArrowLeft' || code === 'ArrowRight') && !isInput) {
      e.preventDefault()
      const curIdx = activeSlot ? orderedSlots.findIndex(s => s.key === activeSlot) : -1
      const nextIdx = wrapIndex(orderedSlots.length, curIdx, code === 'ArrowLeft' ? -1 : 1)
      if (nextIdx < 0) return
      kbSwitch(orderedSlots[nextIdx].key)
      return
    }

    // Skip remaining shortcuts if user is in an input field
    if (isInput) return

    // Panel navigation (core panels + any downstream-registered ones). Core
    // entries are spread last so a stray extension can never shadow them —
    // registerPanelShortcut already rejects core-colliding codes, this is
    // belt-and-suspenders.
    const panelMap: Record<string, string> = { ...EXTRA_PANEL_ROUTES, ...CORE_PANEL_MAP }
    if (!e.shiftKey && panelMap[code]) {
      e.preventDefault()
      navigate(panelMap[code])
      return
    }
  }, [dispatch, navigate, guardedHistoryStep, appStore, onToggleShortcutsModal, onNewChat, onCycleAgent, onCyclePrevAgent, onCycleReasoningEffort, onCyclePrevReasoningEffort, onCycleApprovalMode, onCyclePrevApprovalMode, onCycleModel, onCyclePrevModel, onToggleFocusMode, panelToggleActions, disabled, enabled, ctrlDigits, panelBindings, bindings])

  // Escape stops in-progress voice read-back. CAPTURE phase so it runs before the command palette's bubble-phase Escape
  // handler, which stopPropagation()s and would otherwise close the palette while
  // speech kept playing. Does NOT preventDefault/stopPropagation: Escape must
  // still close whatever it normally closes. Fires the existing `voice-stop`
  // window event (handled by useWebSocket's stopVoice: pause active <audio>,
  // drop queued chunks, clear voicePlaying). No-op unless audio is actually
  // playing and shortcuts are enabled; voicePlaying is read live from the store,
  // and this hook mounts once at the App root, so one listener covers every page.
  // Deliberately ignores `disabled` (set while the shortcuts modal is open):
  // Escape should stop speech consistently from any overlay, including the
  // modal that advertises the shortcut. Only the global enable toggle gates it.
  useEffect(() => {
    if (!enabled) return
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (!appStore.getState().chat.voicePlaying) return
      window.dispatchEvent(new Event('voice-stop'))
    }
    document.addEventListener('keydown', onEsc, true)
    return () => document.removeEventListener('keydown', onEsc, true)
  }, [enabled, appStore])

  // Skip-shell panel toggles (`PANEL_TOGGLES_SKIPPING_SHELL`) need a CAPTURE-phase
  // listener, and only while a terminal holds focus.
  //
  // The bubble-phase block above cannot serve them: xterm.js consumes the keys it
  // recognises before a document listener runs, and on Windows/Linux a `mod` chord
  // IS a control code it recognises — Ctrl+J is ^J (line feed). Verified live: the
  // chord opened the panel, focus landed in the shell, and pressing it again sent a
  // newline to the PTY instead of closing. VS Code has the same problem and solves
  // it inside its own terminal key handler (`commandsToSkipShell` is consulted
  // there, not at the workbench edge); a capture-phase document listener is the
  // equivalent seam here, and it also stops the keystroke reaching the shell.
  //
  // Scoped to terminal targets on purpose: everywhere else the bubble path still
  // owns these chords, so `defaultPrevented` deference and ordering are unchanged.
  useEffect(() => {
    if (!enabled || disabled) return
    const onSkipShell = (e: KeyboardEvent) => {
      if (!isTerminalTarget(e.target)) return
      const id = matchPanelToggleEvent(e, panelBindings)
      if (!id || !PANEL_TOGGLES_SKIPPING_SHELL.has(id)) return
      // An unbound panel (no action) is not ours to claim — see
      // `panelToggleActions`. Checked BEFORE preventDefault so the keystroke
      // still reaches the shell it was aimed at.
      const action = panelToggleActions[id]
      if (!action) return
      e.preventDefault()
      e.stopPropagation()
      action()
    }
    document.addEventListener('keydown', onSkipShell, true)
    return () => document.removeEventListener('keydown', onSkipShell, true)
  }, [enabled, disabled, panelBindings, panelToggleActions])

  useEffect(() => {
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [handler])
}
