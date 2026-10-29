import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useLocation, useNavigationType } from 'react-router-dom'
import { ArrowLeft, ArrowRight } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { IS_MAC, shortcutDefFromChord, shortcutPlatform, useShortcutBindings } from '../hooks/useKeyboardShortcuts'
import { normalizeChord, shortcutEntry, type Chord } from '../lib/shortcutRegistry'
import { ariaKeyshortcutsFor, useShortcutsEnabled } from '../hooks/useNavShortcutHint'
import { useGuardedHistoryStep } from './NavigationLeaveGuard'
import {
  getRouteHistoryPosition,
  recordRouteNavigation,
  routerEntry,
  subscribeRouteHistoryPosition,
} from '../lib/routeHistoryPosition'
import { routeHistoryMenuEntries, type RouteHistoryMenuEntry } from '../lib/routeHistoryLabels'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from './ui/context-menu'

/**
 * Feed the route-history position store — mounted ONCE inside the router (next
 * to `NavigationBackGuard` in main.tsx), and deliberately not folded into the
 * arrows below: the arrows are desktop-only chrome, while the ⌘/Ctrl+←/→ chords
 * read the same store everywhere, so the reporter must outlive the button pair.
 * Renders nothing.
 */
export function RouteHistoryTracker() {
  const location = useLocation()
  const navigationType = useNavigationType()
  // After commit, keyed on the entry: `location.key` changes on every
  // navigation including same-path pushes, and reading `history.state.idx`
  // after commit is what `NavigationBackGuard`'s own tracking effect does.
  useEffect(() => {
    recordRouteNavigation(navigationType)
  }, [location.key, navigationType])
  return null
}

/**
 * Browser-style Back/Forward over the app's own route history, in the header's
 * left cluster (#8258).
 *
 * The dashboard's stack is deliberately rich — desktop session switches PUSH
 * entries (see `sessionUrlHistory.ts`), so Back retraces sessions as well as
 * pages — but until now the only ways to walk it were browser chrome (absent in
 * the desktop app) or a hardware mouse button. These arrows are the visible,
 * surface-independent affordance; ⌘/Ctrl+←/→ in `useKeyboardShortcuts` is the
 * keyboard twin, and both land on the identical `navigate(±1)` path.
 *
 * `useGuardedHistoryStep` on purpose, not bare `navigate(±1)` and not
 * `useGuardedLeave`: a pop is guarded by `NavigationBackGuard`'s trap when one
 * is armed (asking here too would double-prompt), but the trap is best-effort —
 * it stays out of the stack while a Forward branch exists and after a reload —
 * and an in-app control must not fall into that gap and silently discard a
 * draft. The step helper asks exactly once per click, wherever the ask lives.
 *
 * Disabled states come from `routeHistoryPosition` and under-report Forward by
 * design (see that module's header). Two icon buttons are within the
 * `max-two-buttons-per-row` cap, and pagination-style arrows are its named
 * exemption besides.
 */
export function NavHistoryArrows() {
  const step = useGuardedHistoryStep()
  const pos = useSyncExternalStore(subscribeRouteHistoryPosition, getRouteHistoryPosition)
  // Chord advertisements (aria-keyshortcuts, the chord-carrying tooltip) are
  // gated on the same shortcuts-enabled snapshot the rail hints read: the
  // keydown handler bails while shortcuts are off, and advertising a chord
  // that has been switched off would teach a keypress that does nothing — the
  // exact reason useNavShortcutHint returns null in that state.
  const shortcutsEnabled = useShortcutsEnabled()
  // LIVE bindings, not the factory defaults: the registry is user-rebindable
  // (#4608), so an advertisement read from DEFAULT_SHORTCUTS would keep
  // teaching the default chord after the user rebound or unbound it. Same
  // source the keydown handler dispatches from, so hint and binding cannot
  // disagree.
  const bindings = useShortcutBindings()
  const backChord = shortcutsEnabled ? bindings['history-back']?.primary ?? null : null
  const forwardChord = shortcutsEnabled ? bindings['history-forward']?.primary ?? null : null
  const backDef = backChord ? shortcutDefFromChord({ id: 'history-back', group: 'actions' }, backChord) : null
  const forwardDef = forwardChord ? shortcutDefFromChord({ id: 'history-forward', group: 'actions' }, forwardChord) : null
  // The tooltip's chord lives INSIDE the catalog string ("Back · Ctrl+←" /
  // "Zurück · Strg+←"), one key per platform — the focus-mode toggle's
  // pattern: composing label + formatShortcut() at render leaks raw Latin
  // past the pseudolocale (i18n render gate) and hardcodes modifier names
  // locales rename. A catalog string can only spell the FACTORY chord, so the
  // chord-carrying title is shown while the live binding still IS the factory
  // default; a rebound chord gets the bare label (its real chord still
  // reaches assistive tech through aria-keyshortcuts, which is derived live).
  const isFactory = (chord: Chord | null, id: 'history-back' | 'history-forward') => {
    const d = shortcutEntry(id)?.defaults[shortcutPlatform()]
    return !!chord && !!d && JSON.stringify(normalizeChord(chord)) === JSON.stringify(normalizeChord(d))
  }
  const backTitle = isFactory(backChord, 'history-back')
    ? i18nT(IS_MAC ? 'app.nav_back_title_mac' : 'app.nav_back_title')
    : i18nT('app.nav_back')
  const forwardTitle = isFactory(forwardChord, 'history-forward')
    ? i18nT(IS_MAC ? 'app.nav_forward_title_mac' : 'app.nav_forward_title')
    : i18nT('app.nav_forward')
  // `disabled:pointer-events-none` is load-bearing, not cosmetic: a disabled
  // button still RECEIVES pointer events (they just fire no click), so it
  // swallows the mousedown that every outside-click dismisser listens for at
  // the document. These arrows sit at the window's top-left — the exact spot
  // tests and users reach for as "somewhere neutral" (the F12 popover e2e
  // clicks (20,20)) — so a disabled arrow must be transparent to the pointer,
  // letting the click land on the header and count as outside. No pointer
  // events also means no hover state, so no disabled:hover resets are needed.
  const btn = 'flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none text-muted hover:text-text shrink-0 cursor-pointer disabled:opacity-30 disabled:pointer-events-none'
  return (
    <div className="tb-drop-navhistory flex items-center shrink-0" data-testid="nav-history-arrows">
      <HistoryArrow
        direction={-1}
        className={btn}
        disabled={!pos.canGoBack}
        label={i18nT('app.nav_back')}
        title={backTitle}
        keyshortcuts={backDef ? ariaKeyshortcutsFor(backDef) : undefined}
        step={step}
      >
        <ArrowLeft size={15} />
      </HistoryArrow>
      <HistoryArrow
        direction={1}
        className={btn}
        disabled={!pos.canGoForward}
        label={i18nT('app.nav_forward')}
        title={forwardTitle}
        keyshortcuts={forwardDef ? ariaKeyshortcutsFor(forwardDef) : undefined}
        step={step}
      >
        <ArrowRight size={15} />
      </HistoryArrow>
    </div>
  )
}

/**
 * One arrow plus its history menu (#17453), built on the shared
 * `ui/context-menu`: a right-click, the menu key, Shift+F10 or a touch/pen
 * long press opens a named list of the destinations behind or ahead of it.
 * Dismissal, focus return and the long-press timer are the shared menu's.
 *
 * The list is read when the menu opens, so its steps describe the stack as it
 * is then. Another navigation while it is open (browser Back, a chord) would
 * make those steps stale, so the menu is remounted closed instead.
 */
function HistoryArrow({ direction, className, disabled, label, title, keyshortcuts, step, children }: {
  direction: -1 | 1
  className: string
  disabled: boolean
  label: string
  title: string
  keyshortcuts: string | undefined
  step: (delta: number) => void
  children: ReactNode
}) {
  const [entries, setEntries] = useState<RouteHistoryMenuEntry[]>([])
  const [epoch, setEpoch] = useState(0)
  const isOpen = useRef(false)
  const openedAt = useRef<number | null>(null)
  // The release of the long press that opened the menu is followed by a
  // click; that click must not also step. Cleared by the next press or key.
  const skipClick = useRef(false)
  // A touch or pen press is down on the arrow.
  const pressing = useRef(false)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const locationKey = useLocation().key
  useEffect(() => {
    if (isOpen.current) {
      isOpen.current = false
      setEpoch(e => e + 1)
    }
  }, [locationKey])
  // Nothing named to offer: no empty menu, and no browser menu either.
  const hasEntries = routeHistoryMenuEntries(direction).length > 0
  const onOpenChange = (open: boolean) => {
    isOpen.current = open
    if (!open) return
    openedAt.current = routerEntry().idx
    setEntries(routeHistoryMenuEntries(direction))
    // Only a long press is followed by a click; a right-click or the menu
    // key is not, so it must not swallow the next activation.
    skipClick.current = pressing.current
  }
  return (
    <ContextMenu key={epoch} onOpenChange={onOpenChange}>
      {/* No aria-haspopup: the arrow's own activation is a step, not "open a
          menu", and announcing it as a menu button would promise Enter opens one. */}
      <ContextMenuTrigger asChild disabled={!hasEntries}>
        <button
          type="button"
          ref={buttonRef}
          className={className}
          onClick={() => {
            if (skipClick.current) { skipClick.current = false; return }
            step(direction)
          }}
          onPointerDown={e => { skipClick.current = false; pressing.current = e.pointerType !== 'mouse' }}
          onPointerUp={() => { pressing.current = false }}
          onPointerCancel={() => { pressing.current = false }}
          onKeyDown={() => { skipClick.current = false }}
          onContextMenu={e => { if (!hasEntries) e.preventDefault() }}
          disabled={disabled}
          aria-label={label}
          title={title}
          aria-keyshortcuts={keyshortcuts}
        >
          {children}
        </button>
      </ContextMenuTrigger>
      <ContextMenuContent
        aria-label={i18nT(direction === -1 ? 'app.nav_back_history' : 'app.nav_forward_history')}
        data-testid="nav-history-menu"
        // Hand focus back to the arrow, so a keyboard user who opened the
        // list with the menu key is not stranded on <body>.
        onCloseAutoFocus={e => { e.preventDefault(); buttonRef.current?.focus() }}
        className="min-w-[200px] max-w-[min(320px,calc(100vw-16px))]"
      >
        {entries.map(entry => (
          <ContextMenuItem
            key={entry.delta}
            className="[@media(pointer:coarse)]:min-h-11"
            // A delta is only true for the entry the menu was read on.
            onSelect={() => { if (openedAt.current === routerEntry().idx) step(entry.delta) }}
          >
            <span className="truncate">{entry.label}</span>
          </ContextMenuItem>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  )
}
