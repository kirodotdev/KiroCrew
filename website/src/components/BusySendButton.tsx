import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ArrowUpFromLine, Check, ChevronDown, Target } from 'lucide-react'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'

/** Send behavior while the composer is BUSY — a running turn, or background sub-agents
 *  still running for the slot. 'steer' (default) acts on the text immediately (injecting
 *  into a live turn, or starting one); 'queue' defers it to the next turn. */
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
  steer: 'components.chatInput.steer_act_on_this_right_away_desc',
  queue: 'components.chatInput.queue_run_after_the_current_work_finishes_desc',
}
const BUSY_SEND_MODES: Array<{ mode: BusySendMode; icon: React.ReactNode }> = [
  { mode: 'steer', icon: <Target size={15} /> },
  { mode: 'queue', icon: <ArrowUpFromLine size={15} /> },
]

/** Storage key for one slot's preference. A slot-less consumer gets a scoped
 *  sentinel key rather than the legacy unscoped one: the legacy key is a READ-ONLY
 *  migration source (see readBusySendMode), because a live write to it would
 *  change the inherited default of every slot that never chose a mode — the exact
 *  cross-session leak this scoping exists to prevent. */
function busySendModeKey(slotKey?: string | null): string {
  return `${BUSY_SEND_MODE_LS_KEY}:${slotKey || 'no-slot'}`
}

export function readBusySendMode(slotKey?: string | null): BusySendMode {
  const scoped = safeGetItem(busySendModeKey(slotKey))
  if (scoped !== null) return scoped === 'queue' ? 'queue' : 'steer'
  // Migration fallback: before per-slot scoping the preference lived under the
  // unscoped key. A slot that has never chosen a mode inherits that value, so
  // an existing "queue" user keeps their default instead of being reset.
  return safeGetItem(BUSY_SEND_MODE_LS_KEY) === 'queue' ? 'queue' : 'steer'
}

/** Live subscribers to the persisted mode, grouped by storage key. "What does
 *  Enter do while busy" is a PER-SLOT preference: the composers sharing one slot
 *  (main chat and its side panel) must move together the moment it changes —
 *  localStorage alone only syncs across tabs, never within one — while composers
 *  bound to OTHER slots must not move at all. */
const modeListeners = new Map<string, Set<(m: BusySendMode) => void>>()

/** Read + write one slot's busy-send preference. Every mounted consumer of the
 *  SAME slot updates on a change from any other; other slots are untouched. */
export function useBusySendMode(slotKey?: string | null): [BusySendMode, (m: BusySendMode) => void] {
  const storageKey = busySendModeKey(slotKey)
  const [mode, setMode] = useState<BusySendMode>(() => readBusySendMode(slotKey))
  // Rebind (a mounted composer switching slots when activeSlot changes) is
  // resolved DURING render — React's adjust-state-on-prop-change pattern — so
  // the previous slot's mode is never painted, not even for the one frame an
  // effect-based re-read would leave it visible (and clickable).
  const [boundKey, setBoundKey] = useState(storageKey)
  if (boundKey !== storageKey) {
    setBoundKey(storageKey)
    setMode(readBusySendMode(slotKey))
  }
  useEffect(() => {
    let subs = modeListeners.get(storageKey)
    if (!subs) {
      subs = new Set()
      modeListeners.set(storageKey, subs)
    }
    subs.add(setMode)
    return () => {
      subs.delete(setMode)
      if (subs.size === 0) modeListeners.delete(storageKey)
    }
  }, [storageKey])
  const publish = useCallback((m: BusySendMode) => {
    safeSetItem(storageKey, m)
    const subs = modeListeners.get(storageKey)
    if (subs) for (const fn of subs) fn(m)
  }, [storageKey])
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
        {/* Only the fire half dims when disabled: the caret (mode toggle) stays
            live because picking steer-vs-queue before typing is a real workflow,
            and a dimmed control that still works would read as broken. */}
        <button
          className="w-8 h-8 bg-transparent border-none flex items-center justify-center cursor-pointer disabled:cursor-not-allowed disabled:opacity-40 hover:bg-black/15 transition-all text-inherit"
          onClick={onFire}
          disabled={disabled}
          title={mode === 'steer' ? i18nT('components.chatInput.steer_act_on_this_as_soon_as_possible_enter') : i18nT('components.chatInput.queue_run_after_the_current_work_finishes_enter')}
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
              // `focus-visible` rather than `focus`: focus lands on this row as
              // the menu opens, and a plain `focus:` tint would paint it exactly
              // like the hover state for as long as the menu is open.
              className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover focus-visible:bg-bg-hover focus:outline-none transition-colors cursor-pointer text-left border-none"
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
