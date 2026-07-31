import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot, deleteSlot } from '../store/chatSlice'
import { loadChatConfig } from '../pages/chat/ChatSettings'
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

export interface ShortcutDef {
  id: string
  key: string
  alt?: boolean
  ctrl?: boolean  // When true, uses Ctrl on Mac (instead of alt/Option)
  meta?: boolean  // Cmd on Mac, Ctrl on Windows/Linux
  shift?: boolean
  label: string
  group: 'Chat Navigation' | 'Panel Navigation' | 'Actions' | 'Remote Crews'
}

export const DEFAULT_SHORTCUTS: ShortcutDef[] = [
  // Chat navigation — digits use Ctrl on Mac (Option+number produces characters on non-US keyboards)
  { id: 'chat-1', key: '1', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 1', group: 'Chat Navigation' },
  { id: 'chat-2', key: '2', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 2', group: 'Chat Navigation' },
  { id: 'chat-3', key: '3', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 3', group: 'Chat Navigation' },
  { id: 'chat-4', key: '4', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 4', group: 'Chat Navigation' },
  { id: 'chat-5', key: '5', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 5', group: 'Chat Navigation' },
  { id: 'chat-6', key: '6', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 6', group: 'Chat Navigation' },
  { id: 'chat-7', key: '7', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 7', group: 'Chat Navigation' },
  { id: 'chat-8', key: '8', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 8', group: 'Chat Navigation' },
  { id: 'chat-9', key: '9', alt: !IS_MAC, ctrl: IS_MAC, label: 'Jump to chat 9', group: 'Chat Navigation' },
  { id: 'chat-prev', key: 'ArrowLeft', alt: true, label: 'Previous chat', group: 'Chat Navigation' },
  { id: 'chat-next', key: 'ArrowRight', alt: true, label: 'Next chat', group: 'Chat Navigation' },
  { id: 'chat-prev-bracket', key: '[', meta: true, label: 'Previous chat', group: 'Chat Navigation' },
  { id: 'chat-next-bracket', key: ']', meta: true, label: 'Next chat', group: 'Chat Navigation' },
  { id: 'chat-mru', key: '`', alt: true, label: 'Last visited chat (MRU)', group: 'Chat Navigation' },
  { id: 'chat-mru-back', key: '`', alt: true, shift: true, label: 'Walk back MRU history', group: 'Chat Navigation' },
  // Panel navigation
  { id: 'nav-chat', key: 'c', alt: true, label: 'Chats panel', group: 'Panel Navigation' },
  { id: 'nav-notifications', key: 'n', alt: true, label: 'Notifications panel', group: 'Panel Navigation' },
  { id: 'nav-projects', key: 'p', alt: true, label: 'Projects panel', group: 'Panel Navigation' },
  { id: 'nav-schedule', key: 's', alt: true, label: 'Schedule panel', group: 'Panel Navigation' },
  // Actions
  { id: 'focus-input', key: 'Enter', alt: true, label: 'Focus text input', group: 'Actions' },
  { id: 'new-chat', key: 'n', alt: true, shift: true, label: 'New chat', group: 'Actions' },
  { id: 'close-chat', key: 'w', alt: true, shift: true, label: 'Close session', group: 'Actions' },
  { id: 'shortcuts-modal', key: 'k', alt: true, label: 'Open shortcuts help', group: 'Actions' },
  { id: 'open-settings', key: ',', alt: !IS_MAC, meta: IS_MAC, label: 'Open settings', group: 'Actions' },
  { id: 'cycle-agent', key: 'a', alt: true, shift: true, label: 'Cycle agent', group: 'Actions' },
  { id: 'cycle-prev-agent', key: 'z', alt: true, shift: true, label: 'Previous agent', group: 'Actions' },
  { id: 'cycle-reasoning', key: 'd', alt: true, shift: true, label: 'Cycle reasoning effort', group: 'Actions' },
  { id: 'cycle-prev-reasoning', key: 'c', alt: true, shift: true, label: 'Previous reasoning effort', group: 'Actions' },
  { id: 'cycle-approval', key: 'f', alt: true, shift: true, label: 'Cycle approval mode', group: 'Actions' },
  { id: 'cycle-prev-approval', key: 'v', alt: true, shift: true, label: 'Previous approval mode', group: 'Actions' },
  { id: 'cycle-model', key: 's', alt: true, shift: true, label: 'Cycle model', group: 'Actions' },
  { id: 'cycle-prev-model', key: 'x', alt: true, shift: true, label: 'Previous model', group: 'Actions' },
  { id: 'optimize-prompt', key: 'Enter', meta: true, shift: true, label: 'Optimize prompt', group: 'Actions' },
  // Instance switcher — Cmd on Mac / Ctrl on Win-Linux. 1 = Local, 2..6 = the
  // 1st..5th remote instance, matching the InstanceTabBar left-to-right order.
  // Handled by useInstanceShortcuts (not the Alt-based handler below); listed
  // here so they appear in the shortcuts modal + Settings → Shortcuts.
  { id: 'instance-1', key: '1', meta: true, label: 'Switch to Local', group: 'Remote Crews' },
  { id: 'instance-2', key: '2', meta: true, label: 'Switch to remote crew 1', group: 'Remote Crews' },
  { id: 'instance-3', key: '3', meta: true, label: 'Switch to remote crew 2', group: 'Remote Crews' },
  { id: 'instance-4', key: '4', meta: true, label: 'Switch to remote crew 3', group: 'Remote Crews' },
  { id: 'instance-5', key: '5', meta: true, label: 'Switch to remote crew 4', group: 'Remote Crews' },
  { id: 'instance-6', key: '6', meta: true, label: 'Switch to remote crew 5', group: 'Remote Crews' },
]

/**
 * The instance-switch entries, exported as the single source of truth for
 * useInstanceShortcuts: the handler accepts exactly Digit1..Digit<N> where N =
 * INSTANCE_SHORTCUTS.length, so the chords the modal advertises and the chords
 * the handler claims can never drift apart.
 */
export const INSTANCE_SHORTCUTS = DEFAULT_SHORTCUTS.filter(s => s.group === 'Remote Crews')

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
    label: entry.label,
    group: 'Panel Navigation',
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
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const slotHistory = useAppSelector(s => s.chat.slotHistory)
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

    // On Mac (when Ctrl+digit mode enabled), Ctrl+digit switches chats.
    // Check for that first, before the Alt-based gate.
    const code = e.code
    if (ctrlDigits && e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey
        && code >= 'Digit1' && code <= 'Digit9') {
      if (!enabled || disabled) return
      const idx = parseInt(code.charAt(5)) - 1
      e.preventDefault()
      if (idx < slots.length) { dispatch(switchSlot(slots[idx].key)); navigate('/chat') }
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
      const nextIdx = wrapIndex(slots.length, activeSlot ? slots.findIndex(s => s.key === activeSlot) : -1, step)
      if (nextIdx >= 0) { dispatch(switchSlot(slots[nextIdx].key)); navigate('/chat') }
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
      document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()
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
      if (idx < slots.length) { dispatch(switchSlot(slots[idx].key)); navigate('/chat') }
      return
    }

    // Alt+←/→: Previous/next chat (skip when in text input to preserve word-jump)
    if ((code === 'ArrowLeft' || code === 'ArrowRight') && !isInput) {
      e.preventDefault()
      const curIdx = activeSlot ? slots.findIndex(s => s.key === activeSlot) : -1
      const nextIdx = wrapIndex(slots.length, curIdx, code === 'ArrowLeft' ? -1 : 1)
      if (nextIdx < 0) return
      dispatch(switchSlot(slots[nextIdx].key))
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
  }, [dispatch, navigate, slots, activeSlot, slotHistory, onToggleShortcutsModal, onNewChat, onCycleAgent, onCyclePrevAgent, onCycleReasoningEffort, onCyclePrevReasoningEffort, onCycleApprovalMode, onCyclePrevApprovalMode, onCycleModel, onCyclePrevModel, disabled, enabled, ctrlDigits])

  useEffect(() => {
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [handler])
}
