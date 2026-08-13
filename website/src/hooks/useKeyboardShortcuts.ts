import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../store'
import { switchSlot, deleteSlot, openActivityToTab } from '../store/chatSlice'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { queryComposer } from '../pages/chat/composerFocus'
import { reportSeamCollision } from '../apps/seamCollision'
import { i18nT } from '../i18n/t'

export const SHORTCUTS_ENABLED_KEY = 'mc-keyboard-shortcuts'
export const SHORTCUTS_ENABLED_EVENT = 'mc-keyboard-shortcuts-changed'
export const MAC_CTRL_DIGITS_KEY = 'mc-mac-ctrl-digits'

/** True on macOS where Option+number produces characters (UK: ⌥3→#, ⌥2→€). */
export const IS_MAC = /Mac|iPhone|iPad/.test(navigator?.platform ?? '') || /Macintosh/.test(navigator?.userAgent ?? '')

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
 * Which section of the shortcuts reference an entry belongs to.
 *
 * A STABLE, NON-LOCALISED ID — deliberately not the displayed heading. The value is
 * a discriminant first and a label never: `INSTANCE_SHORTCUTS` below and
 * `groupShortcuts()` in ShortcutsModal both select entries by comparing it, so a
 * localised value would make every one of those comparisons miss in every
 * non-English locale and silently render an empty shortcuts modal. The heading text
 * lives in `SHORTCUT_GROUP_LABEL_KEY` and resolves per render.
 */
export type ShortcutGroup = 'chat-navigation' | 'panel-navigation' | 'actions' | 'remote-crews'

/** Shortcut groups in display order — the canonical id set and ordering. */
export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  'chat-navigation', 'panel-navigation', 'actions', 'remote-crews',
]

export interface ShortcutDef {
  id: string
  key: string
  alt?: boolean
  // Literal Ctrl on EVERY platform (rendered ⌃ on Mac, "Ctrl" elsewhere — see
  // formatShortcut). The chat-jump digits set this to `IS_MAC` so that Mac alone
  // uses Ctrl instead of Option; a chord that sets it unconditionally (agent
  // monitor) is Ctrl everywhere. For the ⌘-on-Mac/Ctrl-elsewhere shape use `meta`.
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
}

export const DEFAULT_SHORTCUTS: ShortcutDef[] = [
  // Chat navigation — digits use Ctrl on Mac (Option+number produces characters on non-US keyboards)
  { id: 'chat-1', key: '1', alt: !IS_MAC, ctrl: IS_MAC, n: 1, group: 'chat-navigation' },
  { id: 'chat-2', key: '2', alt: !IS_MAC, ctrl: IS_MAC, n: 2, group: 'chat-navigation' },
  { id: 'chat-3', key: '3', alt: !IS_MAC, ctrl: IS_MAC, n: 3, group: 'chat-navigation' },
  { id: 'chat-4', key: '4', alt: !IS_MAC, ctrl: IS_MAC, n: 4, group: 'chat-navigation' },
  { id: 'chat-5', key: '5', alt: !IS_MAC, ctrl: IS_MAC, n: 5, group: 'chat-navigation' },
  { id: 'chat-6', key: '6', alt: !IS_MAC, ctrl: IS_MAC, n: 6, group: 'chat-navigation' },
  { id: 'chat-7', key: '7', alt: !IS_MAC, ctrl: IS_MAC, n: 7, group: 'chat-navigation' },
  { id: 'chat-8', key: '8', alt: !IS_MAC, ctrl: IS_MAC, n: 8, group: 'chat-navigation' },
  { id: 'chat-9', key: '9', alt: !IS_MAC, ctrl: IS_MAC, n: 9, group: 'chat-navigation' },
  { id: 'chat-prev', key: 'ArrowLeft', alt: true, group: 'chat-navigation' },
  { id: 'chat-next', key: 'ArrowRight', alt: true, group: 'chat-navigation' },
  { id: 'chat-prev-bracket', key: '[', meta: true, group: 'chat-navigation' },
  { id: 'chat-next-bracket', key: ']', meta: true, group: 'chat-navigation' },
  { id: 'chat-mru', key: '`', alt: true, group: 'chat-navigation' },
  { id: 'chat-mru-back', key: '`', alt: true, shift: true, group: 'chat-navigation' },
  // Panel navigation
  { id: 'nav-chat', key: 'c', alt: true, group: 'panel-navigation' },
  { id: 'nav-notifications', key: 'n', alt: true, group: 'panel-navigation' },
  { id: 'nav-projects', key: 'p', alt: true, group: 'panel-navigation' },
  { id: 'nav-schedule', key: 's', alt: true, group: 'panel-navigation' },
  // Actions
  { id: 'focus-input', key: 'Enter', alt: true, group: 'actions' },
  { id: 'new-chat', key: 'n', alt: true, shift: true, group: 'actions' },
  { id: 'close-chat', key: 'w', alt: true, shift: true, group: 'actions' },
  { id: 'shortcuts-modal', key: 'k', alt: true, group: 'actions' },
  { id: 'open-settings', key: ',', alt: !IS_MAC, meta: IS_MAC, group: 'actions' },
  { id: 'cycle-agent', key: 'a', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-prev-agent', key: 'z', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-reasoning', key: 'd', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-prev-reasoning', key: 'c', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-approval', key: 'f', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-prev-approval', key: 'v', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-model', key: 's', alt: true, shift: true, group: 'actions' },
  { id: 'cycle-prev-model', key: 'x', alt: true, shift: true, group: 'actions' },
  { id: 'optimize-prompt', key: 'Enter', meta: true, shift: true, group: 'actions' },
  // Literal Ctrl on every platform — see isAgentMonitorChord for why this one
  // does NOT follow the ⌘-on-Mac convention.
  { id: 'agent-monitor', key: 'g', ctrl: true, group: 'actions' },
  // Instance switcher — Cmd on Mac / Ctrl on Win-Linux. 1 = Local, 2..6 = the
  // 1st..5th remote instance, matching the InstanceTabBar left-to-right order.
  // Handled by useInstanceShortcuts (not the Alt-based handler below); listed
  // here so they appear in the shortcuts modal + Settings → Shortcuts.
  { id: 'instance-1', key: '1', meta: true, group: 'remote-crews' },
  { id: 'instance-2', key: '2', meta: true, n: 1, group: 'remote-crews' },
  { id: 'instance-3', key: '3', meta: true, n: 2, group: 'remote-crews' },
  { id: 'instance-4', key: '4', meta: true, n: 3, group: 'remote-crews' },
  { id: 'instance-5', key: '5', meta: true, n: 4, group: 'remote-crews' },
  { id: 'instance-6', key: '6', meta: true, n: 5, group: 'remote-crews' },
]

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
  'focus-input': 'hooks.useKeyboardShortcuts.focus_text_input',
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
  // Reused: the ChatInput control this chord fires.
  'optimize-prompt': 'components.chatInput.optimize_prompt',
  'agent-monitor': 'hooks.useKeyboardShortcuts.open_agent_monitor',
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

const isMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/**
 * True when `e` is the platform's "open Settings" chord.
 *
 * macOS uses ⌘+, — the OS-standard Preferences chord, and the same one the
 * desktop app's "Settings…" menu item advertises (electron/app-menu.js binds
 * `CmdOrCtrl+,`). Windows/Linux uses Alt+,, matching every other in-page
 * shortcut there.
 *
 * Option+, remains accepted on macOS, unadvertised: a Mac browser can claim
 * ⌘+, as its own Preferences accelerator before the page ever sees the keydown,
 * so dropping the Option chord would leave those users with no keyboard route
 * to Settings. Exactly one primary modifier is required either way, so the
 * chord can't fire from ⌘⌥, or ⌃, misses.
 *
 * `mac` is injectable so both platform behaviours are testable without
 * reloading the module (IS_MAC is fixed at module load).
 */
export function isSettingsChord(
  e: Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  mac: boolean = IS_MAC,
): boolean {
  if (e.code !== 'Comma' || e.shiftKey || e.ctrlKey) return false
  const altOnly = e.altKey && !e.metaKey
  return mac ? (e.metaKey && !e.altKey) || altOnly : altOnly
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

export function formatShortcut(def: ShortcutDef): string {
  const mac = isMac()
  const parts: string[] = []
  if (def.meta) parts.push(mac ? '\u2318' : 'Ctrl')
  if (def.ctrl) parts.push(mac ? '\u2303' : 'Ctrl')
  if (def.alt) parts.push(mac ? '\u2325' : 'Alt')
  if (def.shift) parts.push(mac ? '\u21e7' : 'Shift')
  const keyLabel = def.key === 'ArrowLeft' ? '\u2190' : def.key === 'ArrowRight' ? '\u2192' : def.key === '`' ? '`' : def.key === 'Enter' ? (mac ? '\u23ce' : 'Enter') : def.key === ',' ? ',' : def.key.toUpperCase()
  parts.push(keyLabel)
  return parts.join(mac ? '' : ' + ')
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
  disabled?: boolean
}

export function useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, onCycleAgent, onCyclePrevAgent, onCycleReasoningEffort, onCyclePrevReasoningEffort, onCycleApprovalMode, onCyclePrevApprovalMode, onCycleModel, onCyclePrevModel, disabled }: UseKeyboardShortcutsOpts) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const appStore = useAppStore()
  const mruIndexRef = useRef(-1)
  // Set true right after a char-producing Alt shortcut (Alt+`) fires inside a
  // text field. On macOS those combos are dead keys (Option+` = grave accent),
  // and keydown.preventDefault() cannot cancel the composed character — it
  // arrives via beforeinput. The guard below eats it.
  const suppressNextInputRef = useRef(false)
  const [enabled, setEnabled] = useState(() => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
  const [ctrlDigits, setCtrlDigits] = useState(() => getCtrlDigitsEnabled())

  // Listen for toggle changes from Settings
  useEffect(() => {
    const onToggle = () => {
      setEnabled(localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
      setCtrlDigits(getCtrlDigitsEnabled())
    }
    window.addEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
    return () => window.removeEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
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

    // On Mac (when Ctrl+digit mode enabled), Ctrl+digit switches chats.
    // Check for that first, before the Alt-based gate.
    const code = e.code
    if (ctrlDigits && e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey
        && code >= 'Digit1' && code <= 'Digit9') {
      if (!enabled || disabled) return
      const idx = parseInt(code.charAt(5)) - 1
      e.preventDefault()
      if (idx < orderedSlots.length) { dispatch(switchSlot(orderedSlots[idx].key)); navigate('/chat') }
      return
    }

    // Settings — ⌘+, on macOS, Alt+, on Windows/Linux (see isSettingsChord for
    // why, and for the Option+, fallback Mac browsers still need). Handled
    // BEFORE the Alt gate below because the Mac chord carries no Alt. Fires
    // even when shortcuts are globally disabled, so the user can always reach
    // the toggle that re-enables them. The `code` test is the cheap fast path
    // that keeps the predicate off the hot keystroke path.
    if (code === 'Comma' && isSettingsChord(e)) {
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
      if (nextIdx >= 0) { dispatch(switchSlot(orderedSlots[nextIdx].key)); navigate('/chat') }
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

    // All other shortcuts use Alt (Option on Mac)
    if (!e.altKey || e.ctrlKey || e.metaKey) return

    // Alt+K: Shortcuts modal — always works, even when disabled or in input
    if (code === 'KeyK' && !e.shiftKey) {
      e.preventDefault()
      onToggleShortcutsModal()
      return
    }

    // Suppress all shortcuts when globally disabled via settings
    if (!enabled) return

    // Suppress all other shortcuts when disabled (e.g. modal open)
    if (disabled) return

    // Alt+Shift+A: Cycle agent
    if (e.shiftKey && code === 'KeyA') {
      e.preventDefault()
      onCycleAgent?.()
      return
    }

    // Alt+Shift+Z: Previous agent
    if (e.shiftKey && code === 'KeyZ') { e.preventDefault(); onCyclePrevAgent?.(); return }

    // Alt+Shift+D: Cycle reasoning effort
    if (e.shiftKey && code === 'KeyD') {
      e.preventDefault()
      onCycleReasoningEffort?.()
      return
    }

    // Alt+Shift+C: Previous reasoning effort
    if (e.shiftKey && code === 'KeyC') { e.preventDefault(); onCyclePrevReasoningEffort?.(); return }

    // Alt+Shift+F: Cycle approval mode
    if (e.shiftKey && code === 'KeyF') { e.preventDefault(); onCycleApprovalMode?.(); return }

    // Alt+Shift+V: Previous approval mode
    if (e.shiftKey && code === 'KeyV') { e.preventDefault(); onCyclePrevApprovalMode?.(); return }

    // Alt+Shift+S: Cycle model
    if (e.shiftKey && code === 'KeyS') { e.preventDefault(); onCycleModel?.(); return }
    // Alt+Shift+X: Previous model
    if (e.shiftKey && code === 'KeyX') { e.preventDefault(); onCyclePrevModel?.(); return }

    // Alt+Enter: Focus text input — works even from other inputs
    if (code === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      // Synchronous and unguarded on purpose: no state change precedes this, so
      // there is no next-frame commit to wait for, and a pressed keyboard
      // shortcut proves a keyboard exists — the helper's touch-device skip
      // would wrongly no-op it.
      queryComposer()?.focus()
      return
    }

    // Alt+Shift+N: New chat (check before Alt+N panel nav)
    if (e.shiftKey && code === 'KeyN') {
      e.preventDefault()
      onNewChat()
      return
    }

    // Alt+Shift+W: Close current session (same semantics as the header-menu
    // close — gated by confirmCloseSession, dispatches deleteSlot)
    if (e.shiftKey && code === 'KeyW') {
      e.preventDefault()
      if (activeSlot && (!loadChatConfig().confirmCloseSession || confirm(i18nT('hooks.useKeyboardShortcuts.close_this_session')))) {
        dispatch(deleteSlot(activeSlot))
      }
      return
    }

    // Alt+Shift+`: Walk back MRU history
    if (e.shiftKey && code === 'Backquote') {
      e.preventDefault()
      suppressNextInputRef.current = true
      setTimeout(() => { suppressNextInputRef.current = false }, 0)
      if (slotHistory.length === 0) return
      mruIndexRef.current = Math.min(mruIndexRef.current + 1, slotHistory.length - 1)
      const target = slotHistory[slotHistory.length - 1 - mruIndexRef.current]
      if (target) { dispatch(switchSlot(target)); navigate('/chat') }
      return
    }

    // Alt+`: MRU toggle (last visited)
    if (code === 'Backquote' && !e.shiftKey) {
      e.preventDefault()
      suppressNextInputRef.current = true
      setTimeout(() => { suppressNextInputRef.current = false }, 0)
      const prev = slotHistory.length > 0 ? slotHistory[slotHistory.length - 1] : null
      if (prev && prev !== activeSlot) { dispatch(switchSlot(prev)); navigate('/chat') }
      return
    }

    // Alt+1-9: Jump to chat N (when NOT in Ctrl+digit mode)
    if (!ctrlDigits && code >= 'Digit1' && code <= 'Digit9' && !e.shiftKey) {
      const idx = parseInt(code.charAt(5)) - 1
      e.preventDefault()
      if (idx < orderedSlots.length) { dispatch(switchSlot(orderedSlots[idx].key)); navigate('/chat') }
      return
    }

    // Alt+←/→: Previous/next chat (skip when in text input to preserve word-jump)
    if ((code === 'ArrowLeft' || code === 'ArrowRight') && !isInput) {
      e.preventDefault()
      const curIdx = activeSlot ? orderedSlots.findIndex(s => s.key === activeSlot) : -1
      const nextIdx = wrapIndex(orderedSlots.length, curIdx, code === 'ArrowLeft' ? -1 : 1)
      if (nextIdx < 0) return
      dispatch(switchSlot(orderedSlots[nextIdx].key))
      navigate('/chat')
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
  }, [dispatch, navigate, appStore, onToggleShortcutsModal, onNewChat, onCycleAgent, onCyclePrevAgent, onCycleReasoningEffort, onCyclePrevReasoningEffort, onCycleApprovalMode, onCyclePrevApprovalMode, onCycleModel, onCyclePrevModel, disabled, enabled, ctrlDigits])

  useEffect(() => {
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [handler])
}
