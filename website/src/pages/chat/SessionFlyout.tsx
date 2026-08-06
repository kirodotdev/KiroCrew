import { forwardRef, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { MutableRefObject } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Plus } from 'lucide-react'
import type { ChatSlot } from '../../types'
import { comparePinnedThenSort } from './sessionOrder'
import { i18nT } from '../../i18n/t'

/** Rows shown before the list defers to "show all". Sized so the flyout stays
 *  a glance rather than a panel: past ~8 rows the eye has to scan, at which
 *  point the real sidebar is the better surface and the last row says so. */
export const FLYOUT_MAX_ROWS = 8

/**
 * The sidebar toggle button's rect in container space — the single origin every
 * surface in this interaction grows out of and collapses back into.
 *
 * One definition because three animations must agree on it: the drawer's
 * collapse clip, the drawer's expand clip, and this flyout's open clip. When it
 * was inline at each call site, "the same button" was three literals that could
 * drift. Keep it in sync with the button's own Tailwind classes in ChatPage
 * (`top-[9px] left-2 w-7 h-7`).
 */
export const TOGGLE_RECT = { x: 8, y: 9, size: 28 } as const

/** Clip window covering exactly the toggle button, in the flyout's own box.
 *  The flyout's origin IS the container origin, so container and local
 *  coordinates coincide and TOGGLE_RECT can be used verbatim.
 *
 *  Every side is clamped at 0: a negative inset is invalid CSS and silently
 *  drops the whole clip, which would flash the surface at full size. Both
 *  degenerate inputs are reachable — a sidebar dragged narrower than the
 *  button's right offset, and the pre-layout frame where the height is 0. */
export function toggleClip(panelWidth: number, surfaceH: number): string {
  const right = Math.max(0, panelWidth - TOGGLE_RECT.x - TOGGLE_RECT.size)
  const bottom = Math.max(0, surfaceH - TOGGLE_RECT.y - TOGGLE_RECT.size)
  return `inset(${TOGGLE_RECT.y}px ${right}px ${bottom}px ${TOGGLE_RECT.x}px round 6px)`
}

/** Clip window covering the whole surface. Radius matches `rounded-xl` so the
 *  corner curvature is continuous with the panel the flyout becomes. */
export const FULL_CLIP = 'inset(0px 0px 0px 0px round 12px)'

interface Props {
  slots: ChatSlot[]
  activeSlot: string | null
  unreadSlots: string[]
  /** Width of the sidebar this flyout expands into. The flyout matches it
   *  exactly, so expanding never moves the left or right edge. */
  panelWidth: number
  /** Vertical space available in the container. Caps the flyout at the height
   *  the expanded panel would have, so it can never overhang the surface it is
   *  about to become, and scrolls instead of clipping in a short window. */
  maxHeight: number
  /** Socket state. Switching while disconnected strands the user on an empty
   *  transcript (switchSlot.rejected clears messages), so rows go inert. */
  connected: boolean
  creating?: boolean
  /** Move focus into the first row on open — only when the keyboard opened it.
   *  Hover must never steal focus from whatever the user is typing in. */
  autoFocus?: boolean
  onSwitch: (key: string) => void
  onNew: () => void
  /** Grow the flyout into the real sidebar. */
  onExpand: () => void
  /** Dismiss without expanding, returning focus to the trigger. */
  onDismiss: () => void
  onMouseEnter?: () => void
  onMouseLeave?: () => void
  /** Focus leaving the flyout for good. Bubbles from the rows, so it fires when
   *  the user tabs out of the surface entirely. */
  onBlur?: (e: React.FocusEvent) => void
}

/** Which status marker a row gets, in the sidebar's own precedence order: an
 *  approval request outranks activity, and activity outranks unread (a running
 *  slot is self-evidently unread). Returns null when the row is quiet — the
 *  slot still reserves the column so titles stay aligned. */
function statusOf(slot: ChatSlot): 'approval' | 'running' | 'unread' | null {
  if (slot.pending_approval) return 'approval'
  if (slot.running) return 'running'
  return null
}

/**
 * Session switcher that opens on hovering the collapsed sidebar's toggle.
 *
 * The collapsed sidebar made switching a three-step chore: expand, switch,
 * collapse again. This is the one-step version — the list arrives under the
 * pointer already ordered by most recent activity, and clicking the toggle
 * grows this same rect into the real sidebar instead of replacing it.
 *
 * It sits exactly where the expanded panel sits — same origin, same width, same
 * header geometry — and is only SHORTER. So expanding moves one edge, the
 * bottom, with the corner the eye is fixated on pinned in place.
 *
 * Deliberately NOT the sidebar rendered small. A row is a status marker and a
 * title, nothing else. No timestamp: the list is already ordered by recency, so
 * a per-row time restates the ordering the position already shows, and it was
 * costing the title a third of the row's width. The three-deck rows with agent,
 * status detail, timestamps and source chips are what expanding gets you.
 * `renderSessionRow` could not be reused anyway — it closes over ~40 pieces of
 * sidebar state plus a DnD context.
 */
const SessionFlyout = forwardRef<HTMLDivElement, Props>(function SessionFlyout({
  slots, activeSlot, unreadSlots, panelWidth, maxHeight, connected, creating,
  autoFocus, onSwitch, onNew, onExpand, onDismiss, onMouseEnter, onMouseLeave, onBlur,
}, forwardedRef) {
  const listRef = useRef<HTMLDivElement>(null)
  // Own handle on the surface for the roving-focus query. The parent also needs
  // it (hover-intent outside-click, and the rect handed to the drawer's expand
  // clip), so the forwarded ref is mirrored rather than replaced.
  const surfaceEl = useRef<HTMLDivElement | null>(null)
  const reduce = useReducedMotion()
  const setSurface = (node: HTMLDivElement | null) => {
    surfaceEl.current = node
    if (typeof forwardedRef === 'function') forwardedRef(node)
    else if (forwardedRef) (forwardedRef as MutableRefObject<HTMLDivElement | null>).current = node
  }

  const unread = useMemo(() => new Set(unreadSlots), [unreadSlots])
  const pinned = useMemo(() => new Set(slots.filter(s => s.pinned).map(s => s.key)), [slots])

  // Always date-desc, regardless of the sidebar's saved sort. This surface is
  // "what was I just doing" — a name-sorted flyout would answer a different
  // question than the one hovering it asks. Pin-first still applies so a row
  // does not change position between the two surfaces.
  const ordered = useMemo(
    () => [...slots].sort((a, b) => comparePinnedThenSort(a, b, 'date-desc', pinned)),
    [slots, pinned],
  )
  const rows = ordered.slice(0, FLYOUT_MAX_ROWS)
  const hidden = ordered.length - rows.length

  useEffect(() => {
    if (!autoFocus) return
    // Focus the ACTIVE row when it is on screen, else the first row: a keyboard
    // user arrives where they already are, so one Down/Up step is a real move.
    const list = listRef.current
    if (!list) return
    const target = list.querySelector<HTMLElement>('[data-slot-key][aria-current="true"]')
      ?? list.querySelector<HTMLElement>('[data-slot-key]')
    target?.focus()
  }, [autoFocus])

  /** Roving focus over every menuitem — the rows plus New and Show all, in DOM
   *  order. Up/Down move instead of Tab (this is a menu, and the trigger stays
   *  the page's single tab stop for it), and both ends wrap so a long hold
   *  cannot dead-end. */
  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return
    const items = Array.from(surfaceEl.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
    if (items.length === 0) return
    e.preventDefault()
    const at = items.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0
      : e.key === 'End' ? items.length - 1
      : e.key === 'ArrowDown' ? (at + 1 + items.length) % items.length
      : (at - 1 + items.length) % items.length
    items[next]?.focus()
  }

  // Roving-focus target on keyboard open, measured height for the open clip.
  const [surfaceH, setSurfaceH] = useState(0)
  // Layout effect, not effect: the height must be known BEFORE the first paint,
  // or the clip animation starts from a stale rect and the growth visibly jumps.
  useLayoutEffect(() => {
    setSurfaceH(surfaceEl.current?.offsetHeight ?? 0)
  }, [rows.length, hidden, panelWidth, maxHeight])

  // Open as the panel's visible WINDOW growing out of the toggle button —
  // the same mechanism, easing and duration as the sidebar's own collapse/expand
  // (OverlayDrawer's clip morph). The previous fade-plus-2%-scale was a generic
  // popover entrance: it animated, but it arrived at full size, so the surface
  // read as "appeared over the button" instead of "came out of it". With this,
  // one continuous language runs the whole interaction: button rect → flyout
  // rect → full panel rect, and back.
  const openClip = surfaceH > 0 && !reduce
    ? { from: toggleClip(panelWidth, surfaceH), to: FULL_CLIP }
    : null

  return (
    <motion.div
      ref={setSurface}
      role="menu"
      aria-label={i18nT('pages.chat.sessionFlyout.recent_sessions')}
      onKeyDown={onMenuKeyDown}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onBlur={onBlur}
      // Occupies the EXPANDED PANEL's rect, top-left included — not a card
      // floating inside it. The panel's own top-left is the container origin,
      // so the flyout is placed there and only its BOTTOM edge is short. That
      // makes expanding a single-edge growth with a pinned corner: nothing the
      // eye is already fixated on moves. Insetting it instead (the first cut)
      // made all four edges travel, which read as a jump rather than a growth.
      // z-[59] is the one free layer: above every chat-pane surface (max z-50),
      // below the drawer's morph (z-[60]) so the growing panel paints over it,
      // and below the toggle (z-[61]) so the button stays clickable throughout.
      className="absolute top-0 left-0 z-[59] flex flex-col overflow-hidden rounded-xl border border-border bg-bg-elevated shadow-lg"
      style={{ width: panelWidth, maxHeight }}
      // Keyframe arrays, not `initial`: the clip's start depends on the measured
      // height, which only exists from the second render, and `initial` is read
      // once at mount. Frame one is opacity 0, so the pre-measure frame is not
      // visible. Reduced motion keeps the fade and drops the growth.
      initial={{ opacity: 0 }}
      animate={openClip
        ? { opacity: [0, 1], clipPath: [openClip.from, openClip.to] }
        : { opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.1 } }}
      transition={reduce
        ? { duration: 0.15 }
        : { duration: 0.24, ease: [0.32, 0.72, 0, 1], opacity: { duration: 0.12 } }}
    >
      {/* Header geometry is the expanded sidebar's header, to the pixel:
          `px-2 mt-0.5 h-10` inside a 1px card border puts the h-7 button at
          y 9..37 — exactly where the toggle button sits, and exactly where the
          real header's New button lands. So across the morph the header does
          not move at all; only the list under it lengthens. `pl-9` on the title
          clears the toggle, which paints on top of this surface at z-[61]. */}
      <div className="flex h-10 shrink-0 items-center justify-between px-2 mt-0.5">
        <div className="flex min-w-0 flex-1 items-center gap-1.5 pl-9">
          {/* The expanded panel's own title, key included — same string, same
              type, same position. Reusing `pages.chatSidebar.sessions` rather
              than a flyout-local copy means the two can never disagree, in any
              locale. A distinct "Recent" caption would put a text swap in the
              middle of a morph whose whole point is that nothing moves. */}
          <span className="sessions-panel-title truncate text-sm font-semibold tracking-[.04em] text-text-strong">
            {i18nT('pages.chatSidebar.sessions')}
          </span>
        </div>
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          onClick={onNew}
          disabled={!connected || creating}
          className="flex h-7 shrink-0 items-center gap-1.5 rounded-md bg-accent pl-2 pr-2.5 text-[12px] font-semibold text-accent-fg transition-all hover:bg-accent-hover active:scale-95 disabled:cursor-not-allowed disabled:opacity-70 disabled:active:scale-100"
          aria-label={i18nT('pages.chat.sessionFlyout.new_chat')}
        >
          <Plus size={15} aria-hidden />
          <span className="whitespace-nowrap">{i18nT('pages.chat.sessionFlyout.new')}</span>
        </button>
      </div>
      <div className="shrink-0 border-t border-border" />

      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {rows.map(slot => {
          const isActive = slot.key === activeSlot
          const status = statusOf(slot)
          const isUnread = !status && unread.has(slot.key)
          const label = slot.title && slot.title !== slot.key ? slot.title : slot.key
          return (
            <button
              key={slot.key}
              type="button"
              role="menuitem"
              // Roving tabindex: the list is one tab stop, Up/Down move within
              // it. -1 keeps rows reachable by script focus without adding
              // eight stops to the page's tab order.
              tabIndex={-1}
              data-slot-key={slot.key}
              aria-current={isActive ? 'true' : undefined}
              // Not `disabled`: a disabled button is unfocusable, which would
              // silently drop rows out of the Up/Down ring while offline. The
              // click handler is the real guard.
              aria-disabled={!connected}
              title={label}
              onClick={() => { if (connected) onSwitch(slot.key) }}
              className={`flex w-full items-center gap-2 rounded-md border-none bg-transparent px-2 py-1.5 text-left text-[13px] outline-none transition-colors ${
                isActive
                  ? '!bg-accent-subtle text-text-strong'
                  : connected
                    ? 'text-text hover:bg-bg-hover focus-visible:bg-bg-hover'
                    : 'text-muted opacity-50'
              } ${connected ? 'cursor-pointer' : 'cursor-not-allowed'}`}
            >
              {/* Marker column is always present, even when quiet, so titles
                  never shift horizontally as slots start and stop running. */}
              <span
                aria-hidden
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  status === 'approval' ? 'bg-warn'
                    : status === 'running' ? 'bg-accent animate-pulse'
                      : isUnread ? 'bg-accent'
                        : 'bg-transparent'
                }`}
              />
              <span className={`min-w-0 flex-1 truncate ${isActive ? 'font-semibold' : ''}`}>{label}</span>
            </button>
          )
        })}

        {hidden > 0 && (
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            onClick={onExpand}
            className="mt-0.5 w-full cursor-pointer rounded-md border-none bg-transparent px-2 py-1.5 text-left text-[12px] font-semibold text-accent transition-colors hover:bg-bg-hover"
          >
            {i18nT('pages.chat.sessionFlyout.show_all_sessions')}
          </button>
        )}
      </div>

      {/* Escape is handled by the hover-intent hook at document level; this row
          just advertises it, and gives the pointer a dismiss target. */}
      <button
        type="button"
        onClick={onDismiss}
        tabIndex={-1}
        aria-hidden
        className="flex shrink-0 cursor-default items-center justify-end gap-1.5 border-t border-border bg-transparent px-3 py-1.5 text-[11px] text-muted"
      >
        <kbd className="rounded border border-border-strong px-1 py-px font-sans text-[10px]">↑↓</kbd>
        <kbd className="rounded border border-border-strong px-1 py-px font-sans text-[10px]">↵</kbd>
        <kbd className="rounded border border-border-strong px-1 py-px font-sans text-[10px]">esc</kbd>
      </button>
    </motion.div>
  )
})

export default SessionFlyout
