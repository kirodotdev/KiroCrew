import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot, deleteSlot } from '../store/chatSlice'
import { loadChatConfig } from '../pages/chat/ChatSettings'

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
  group: 'Chat Navigation' | 'Panel Navigation' | 'Actions' | 'Instances'
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
  { id: 'open-settings', key: ',', alt: true, label: 'Open settings', group: 'Actions' },
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
  { id: 'instance-1', key: '1', meta: true, label: 'Switch to Local', group: 'Instances' },
  { id: 'instance-2', key: '2', meta: true, label: 'Switch to instance 1', group: 'Instances' },
  { id: 'instance-3', key: '3', meta: true, label: 'Switch to instance 2', group: 'Instances' },
  { id: 'instance-4', key: '4', meta: true, label: 'Switch to instance 3', group: 'Instances' },
  { id: 'instance-5', key: '5', meta: true, label: 'Switch to instance 4', group: 'Instances' },
  { id: 'instance-6', key: '6', meta: true, label: 'Switch to instance 5', group: 'Instances' },
]

/**
 * The instance-switch entries, exported as the single source of truth for
 * useInstanceShortcuts: the handler accepts exactly Digit1..Digit<N> where N =
 * INSTANCE_SHORTCUTS.length, so the chords the modal advertises and the chords
 * the handler claims can never drift apart.
 */
export const INSTANCE_SHORTCUTS = DEFAULT_SHORTCUTS.filter(s => s.group === 'Instances')

const isMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

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

    // All other shortcuts use Alt (Option on Mac)
    if (!e.altKey || e.ctrlKey || e.metaKey) return

    // Alt+K: Shortcuts modal — always works, even when disabled or in input
    if (code === 'KeyK' && !e.shiftKey) {
      e.preventDefault()
      onToggleShortcutsModal()
      return
    }

    // Alt+,: Settings — always works so user can re-enable shortcuts
    if (code === 'Comma' && !e.shiftKey) {
      e.preventDefault()
      navigate('/settings')
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
      if (activeSlot && (!loadChatConfig().confirmCloseSession || confirm('Close this session?'))) {
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
      if (slots.length === 0) return
      const curIdx = activeSlot ? slots.findIndex(s => s.key === activeSlot) : -1
      const nextIdx = code === 'ArrowLeft'
        ? (curIdx <= 0 ? slots.length - 1 : curIdx - 1)
        : (curIdx >= slots.length - 1 ? 0 : curIdx + 1)
      dispatch(switchSlot(slots[nextIdx].key))
      navigate('/chat')
      return
    }

    // Skip remaining shortcuts if user is in an input field
    if (isInput) return

    // Panel navigation
    const panelMap: Record<string, string> = { KeyC: '/chat', KeyN: '/notifications', KeyP: '/projects', KeyS: '/schedule' }
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
