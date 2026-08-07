import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ArrowUpFromLine, Check, ChevronDown, Target } from 'lucide-react'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { safeSetItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'

/** Send behavior while a turn is RUNNING. 'steer' (default) injects the
 *  composer into the running turn; 'queue' defers it to the next turn. */
export type BusySendMode = 'steer' | 'queue'

export const BUSY_SEND_MODE_LS_KEY = 'mc-busy-send-mode'

/**
 * Catalog KEYS for the two modes' menu copy.
 *
 * Keys, not strings: these are built at module load, so an `i18nT()` call here
 * would freeze whatever language was active at boot and never re-resolve on a
 * language switch. The lookups happen in the menu's render.
 *
 * Shaped as flat `Record`s of full literal keys, indexed inline at the `i18nT()`
 * call, because that is the only form `scripts/check-i18n-keys.mjs` can resolve
 * statically — nested in the array and read as `i18nT(m.labelKey)` the gate
 * cannot see the key at all.
 *
 * `steer` reuses the label the split button's `aria-label` already ships rather
 * than sending a duplicate English string to ten locales.
 */
const BUSY_SEND_MODE_LABEL_KEY: Record<BusySendMode, string> = {
  steer: 'components.chatInput.steer',
  queue: 'components.chatInput.queue',
}
const BUSY_SEND_MODE_DESC_KEY: Record<BusySendMode, string> = {
  steer: 'components.chatInput.steer_desc',
  queue: 'components.chatInput.queue_desc',
}
const BUSY_SEND_MODES: Array<{ mode: BusySendMode; icon: React.ReactNode }> = [
  { mode: 'steer', icon: <Target size={15} /> },
  { mode: 'queue', icon: <ArrowUpFromLine size={15} /> },
]

export function readBusySendMode(): BusySendMode {
  try { return localStorage.getItem(BUSY_SEND_MODE_LS_KEY) === 'queue' ? 'queue' : 'steer' } catch { return 'steer' }
}

/** Live subscribers to the persisted mode. "What does Enter do while busy" is
 *  ONE user preference, so every composer showing this button (main chat and the
 *  side panel) must move together the moment it changes — localStorage alone
 *  only syncs across tabs, never within one. */
const modeListeners = new Set<(m: BusySendMode) => void>()

/** Read + write the shared busy-send preference. Every mounted consumer updates
 *  on a change from any other consumer. */
export function useBusySendMode(): [BusySendMode, (m: BusySendMode) => void] {
  const [mode, setMode] = useState<BusySendMode>(readBusySendMode)
  useEffect(() => {
    modeListeners.add(setMode)
    return () => { modeListeners.delete(setMode) }
  }, [])
  const publish = useCallback((m: BusySendMode) => {
    safeSetItem(BUSY_SEND_MODE_LS_KEY, m)
    for (const fn of modeListeners) fn(m)
  }, [])
  return [mode, publish]
}

/**
 * The split send button shown while a turn is running: `[ action | ▾ ]`. The
 * main area fires the selected mode (the same action Enter takes); the chevron
 * opens a mode picker.
 *
 * Shared by the main composer and the side panel so the two surfaces cannot
 * drift on the icon, colour, copy, or keyboard semantics of "send while busy".
 */
export default function BusySendButton({
  mode,
  onModeChange,
  onFire,
  disabled = false,
}: {
  mode: BusySendMode
  onModeChange: (m: BusySendMode) => void
  /** Fire the currently selected mode with the composer's text. */
  onFire: () => void
  disabled?: boolean
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [menuRect, setMenuRect] = useState<DOMRect | null>(null)
  const splitRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const caretRef = useRef<HTMLButtonElement>(null)
  // This menu has no filter input; the ref stays null so useListboxKeyboard
  // treats ArrowUp from the first option as a no-op instead of a focus jump.
  const noInputRef = useRef<HTMLElement | null>(null)

  const closeToTrigger = useCallback(() => {
    setMenuOpen(false)
    caretRef.current?.focus()
  }, [])

  // Keyboard operability for the portaled menu (WAI-ARIA menu pattern):
  // focus moves into the first option on open, ArrowUp/Down + Home/End roam,
  // Escape/Tab close and return focus to the caret trigger.
  const { onListKeyDown } = useListboxKeyboard({
    open: menuOpen,
    dropdownRef: menuRef,
    inputRef: noInputRef,
    hasFilterInput: false,
    filteredCount: BUSY_SEND_MODES.length,
    onEnterSingleMatch: () => {},
    closeToTrigger,
  })

  useEffect(() => {
    if (!menuOpen) return
    // Menu is portaled to <body> (escapes the composer's overflow-hidden), so the
    // outside-click guard must exclude both the split button and the menu.
    const h = (e: MouseEvent) => {
      const t = e.target as Node
      if (!splitRef.current?.contains(t) && !menuRef.current?.contains(t)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [menuOpen])

  const toggleMenu = () => {
    if (!menuOpen && splitRef.current) setMenuRect(splitRef.current.getBoundingClientRect())
    setMenuOpen(o => !o)
  }
  const select = (m: BusySendMode) => {
    onModeChange(m)
    closeToTrigger()
  }

  return (
    <div className="relative flex items-center" ref={splitRef}>
      <div className={`flex items-stretch h-8 rounded-full overflow-hidden transition-colors ${mode === 'steer' ? 'bg-accent text-accent-fg' : 'bg-warn text-warn-fg'}`}>
        <button
          className="w-8 h-8 bg-transparent border-none flex items-center justify-center cursor-pointer hover:bg-black/15 transition-all text-inherit"
          onClick={onFire}
          disabled={disabled}
          title={mode === 'steer' ? i18nT('components.chatInput.steer_inject_into_the_running_turn_enter') : i18nT('components.chatInput.queue_run_after_the_current_turn_finishes_enter')}
          aria-label={mode === 'steer' ? i18nT('components.chatInput.steer') : i18nT('components.chatInput.queue_message')}
          data-testid="busy-send-button"
        >
          {mode === 'steer' ? <Target size={16} /> : <ArrowUpFromLine size={16} />}
        </button>
        <div className="w-px my-1.5 bg-current opacity-40" aria-hidden="true" />
        <button
          ref={caretRef}
          className="w-6 h-8 bg-transparent border-none flex items-center justify-center cursor-pointer hover:bg-black/15 transition-all text-inherit"
          onClick={toggleMenu}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-label={i18nT('components.chatInput.send_options')}
          title={i18nT('components.chatInput.send_options')}
          data-testid="busy-send-caret"
        >
          <ChevronDown size={14} className={`transition-transform ${menuOpen ? 'rotate-180' : ''}`} />
        </button>
      </div>
      {menuOpen && menuRect && createPortal(
        <div
          ref={menuRef}
          role="menu"
          onKeyDown={onListKeyDown}
          className="fixed w-[250px] rounded-xl bg-bg-elevated border border-border shadow-xl p-1.5 animate-slide-up z-[60]"
          style={{ left: Math.max(8, Math.min(menuRect.right - 250, window.innerWidth - 250 - 8)), bottom: window.innerHeight - menuRect.top + 8 }}
        >
          {BUSY_SEND_MODES.map(({ mode: m, icon }) => (
            <button
              key={m}
              role="menuitemradio"
              aria-checked={mode === m}
              data-option=""
              tabIndex={-1}
              onClick={() => select(m)}
              className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover focus:bg-bg-hover focus:outline-none transition-colors cursor-pointer text-left border-none"
              data-testid={`busy-send-mode-${m}`}
            >
              <span className={`shrink-0 ${m === 'steer' ? 'text-accent' : 'text-warn'}`}>{icon}</span>
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-medium text-text">{i18nT(BUSY_SEND_MODE_LABEL_KEY[m])}</div>
                <div className="text-[11px] text-muted leading-snug">{i18nT(BUSY_SEND_MODE_DESC_KEY[m])}</div>
              </div>
              {mode === m && <Check size={14} className="text-accent shrink-0" />}
            </button>
          ))}
        </div>,
        document.body
      )}
    </div>
  )
}
