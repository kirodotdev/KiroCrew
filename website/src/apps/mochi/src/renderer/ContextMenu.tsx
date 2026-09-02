/**
 * Reusable context menu component.
 * Used by PetWidget (overlay) and ChatPanel (chat window).
 * Handles edge clamping, click-outside dismiss, and optional hitbox reporting for overlay use.
 */
import React, { useEffect, useLayoutEffect, useRef, useCallback } from 'react'

import { useMenuKeyboard } from '../../../../hooks/useMenuKeyboard'
import { api } from '../mochiApi'

export interface ContextMenuItem {
  label: string
  action: string
  danger?: boolean
  separator?: false
  /**
   * Leading glyph, as a component rather than a character in the label.
   *
   * These rows used to carry an emoji inside the translated string, which made
   * the icon a property of the TEXT: it could not follow the theme, rendered at
   * whatever size and baseline the font chose, and had to be duplicated in every
   * locale. Pass a `lucide-react` component instead — sized explicitly and drawn
   * in `currentColor`, so it inherits the row's colour including the danger case.
   */
  icon?: React.ComponentType<{ size?: number | string; color?: string }>
  /**
   * Optional trailing keyboard-shortcut hint (e.g. "⌘⇧H"), right-aligned and
   * dimmed. Used to make an otherwise-undiscoverable action reachable — e.g. the
   * pet's Hide row, whose restore is a global accelerator (the hidden pet can't
   * be right-clicked to bring itself back).
   */
  shortcut?: string
}

export interface ContextMenuSeparator {
  separator: true
}

export type ContextMenuEntry = ContextMenuItem | ContextMenuSeparator

interface Props {
  x: number
  y: number
  items: ContextMenuEntry[]
  /** If true, reports hitbox to main process for overlay mouse-forward. Default false. */
  reportHitbox?: boolean
  onAction: (action: string) => void
  onClose: () => void
}

const MENU_MIN_W = 160

export function ContextMenu({ x, y, items, reportHitbox, onAction, onClose }: Props) {
  const menuRef = useRef<HTMLDivElement>(null)

  /*
   * The container below carries `role="menu"`, which tells assistive technology
   * that focus is MANAGED here and the arrow keys walk the rows (WAI-ARIA menu
   * pattern) — a promise this component made and then did not keep: the rows
   * were `tabIndex={0}` divs with an Enter/Space handler and nothing else, so a
   * screen-reader user who reached for the arrows got page scroll instead of
   * item navigation (#6231, #5851).
   *
   * `useMenuKeyboard` is the shared spelling of that contract (extracted from
   * `MenuBtn` in DevFleetPage) — ArrowDown/ArrowUp with wrap at both ends,
   * Home/End to the boundary rows, Tab/Shift-Tab contained inside the menu
   * (#2533), and an IME composition latch so a candidate-list arrow is not
   * stolen from the input method. `enabled` is a literal `true` because this
   * component is MOUNTED ONLY WHILE OPEN: its hosts (PetContextMenu, ChatPanel)
   * render it conditionally, so mount is open and unmount is close — there is no
   * separate open flag to gate on, and the hook's cleanup drops the listener.
   *
   * Item discovery is left at the default (`menuItemsOf`): it collects the
   * `role="menuitem"` rows in document order and naturally steps over the
   * `role="separator"` dividers, so no `getItems` override is needed.
   *
   * `focusFirstOnOpen` is left at its default (true), which matches `MenuBtn`'s
   * posture: focus enters the menu on open so the first arrow CHOOSES a row
   * rather than being spent entering the list. That is safe against the
   * close-on-blur effect below because it listens for a WINDOW blur (the pet
   * overlay losing focus), not an element one — moving focus between rows
   * dispatches non-bubbling element blur/focusout and never reaches it.
   *
   * Escape and Enter/Space stay where they are: the hook deliberately owns
   * navigation only, because what "close" means (and the per-row action) is the
   * host's business.
   */
  useMenuKeyboard({ enabled: true, containerRef: menuRef })

  /*
   * The element that was focused when this menu MOUNTED — the row, bubble or
   * pet the user right-clicked (or reached with the keyboard). Captured during
   * the first RENDER on purpose, not in an effect: render runs before
   * `useMenuKeyboard`'s focus-entry effect moves focus onto the first row, so
   * this is the last moment the opener is still `document.activeElement`.
   * Same spelling as `SlotPopover` in PackEditor.tsx, which had this problem
   * first. `undefined` is the "not yet captured" sentinel — `activeElement`
   * itself can in principle be null, and using null for both would re-run the
   * capture on a later render and latch a menu ROW as the opener.
   */
  const opener = useRef<Element | null | undefined>(undefined)
  if (opener.current === undefined) opener.current = document.activeElement

  /*
   * Give focus back when the menu goes away (#6267 review).
   *
   * Focus ENTRY without a matching restore is a regression, not half a feature:
   * every host renders this component conditionally (ChatPanel, PetContextMenu),
   * so closing it UNMOUNTS the element that holds focus, and the browser drops
   * focus to `<body>`. A keyboard user who pressed Escape or picked a row would
   * be left nowhere — the next Tab restarts from the top of the document instead
   * of resuming beside what they right-clicked. Before the rows were focusable
   * nothing was lost on close, so the entry is what created this debt.
   *
   * Doing it in an UNMOUNT cleanup rather than next to each dismissal covers all
   * of them in one place — Escape, row activation via `handleAction`, the
   * close-on-window-blur path, and a host that stops rendering the menu for its
   * own reasons (a re-render, a route change) and never called `onClose` at all.
   *
   * The guard says: restore ONLY if focus is still inside the menu being
   * destroyed — precisely the case where focus would otherwise be lost. If an
   * action legitimately moved focus elsewhere (settings or the dashboard opening
   * and focusing something there), or the menu was dismissed by an outside click
   * that already moved focus, then focus has a rightful owner and reclaiming it
   * would be a second bug wearing the first one's clothes. In practice react-dom
   * ALSO defends that case for us — it snapshots `activeElement` before the
   * mutation phase and re-focuses it after the commit when that node is still in
   * the document, which is exactly why this restore only "wins" when the focused
   * row is being removed. The guard is kept anyway: it states the intent at the
   * site, and it is the only protection if this ever moves off the commit path
   * (an explicit Escape handler, the shape `SlotPopover` uses).
   *
   * The container is read into `menu` AT MOUNT rather than as `menuRef.current`
   * inside the cleanup: React detaches host refs while tearing the tree down,
   * and the captured node answers `contains` just as well.
   *
   * `useLayoutEffect`, NOT `useEffect`, and that is the load-bearing detail.
   * React runs layout cleanups synchronously as it walks the deleted subtree,
   * while the menu is STILL in the document and still holds focus; passive
   * (`useEffect`) cleanups are deferred until after the commit, by which point
   * the DOM node is gone and the browser has already reset `activeElement` to
   * `<body>`. In a passive cleanup the guard therefore reads "focus is not in
   * the menu" every single time and restores nothing — a version of this fix
   * written with `useEffect` looks right, type-checks, and silently does not
   * work. The tests below the fold in CrewCompanionContextMenu.test.tsx are what
   * distinguish the two.
   */
  useLayoutEffect(() => {
    const menu = menuRef.current
    return () => {
      if (menu?.contains(document.activeElement)) {
        ;(opener.current as HTMLElement | null)?.focus?.()
      }
    }
  }, [])

  // Clamp position so menu stays within viewport
  const [clampedX, setClampedX] = React.useState(x)
  const [clampedY, setClampedY] = React.useState(y)

  useEffect(() => {
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const newX = x + rect.width > window.innerWidth ? Math.max(0, x - rect.width) : x
    const newY = y + rect.height > window.innerHeight ? Math.max(0, y - rect.height) : y
    setClampedX(newX)
    setClampedY(newY)
  }, [x, y])

  // Report menu hitbox to main process (overlay only)
  useEffect(() => {
    if (!reportHitbox) return
    const el = menuRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    api?.setMenuHitbox?.({ x: rect.left, y: rect.top, w: rect.width, h: rect.height })
    return () => { api?.setMenuHitbox?.(null) }
  }, [clampedX, clampedY, reportHitbox])

  // Close on click outside, Escape, or window losing focus
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose()
    }
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const handleBlur = () => onClose()

    // Tell main process to capture clicks on all overlays (like drag does)
    // so clicks on other screens dismiss the menu
    if (reportHitbox) {
      // DIVERGENCE: the original used a generic api.send relay; a relay is on
      // the preload's never-expose list, so this uses the dedicated channel.
      api?.menuOpened?.()
    }

    const timer = setTimeout(() => {
      window.addEventListener('mousedown', handleClick, true)
      window.addEventListener('keydown', handleKey, true)
      window.addEventListener('blur', handleBlur)
    }, 50)
    return () => {
      clearTimeout(timer)
      window.removeEventListener('mousedown', handleClick, true)
      window.removeEventListener('keydown', handleKey, true)
      window.removeEventListener('blur', handleBlur)
      if (reportHitbox) {
        api?.menuClosed?.()
      }
    }
  }, [onClose, reportHitbox])

  const handleAction = useCallback((action: string) => {
    onClose()
    onAction(action)
  }, [onClose, onAction])

  return (
    <div
      ref={menuRef}
      role="menu"
      style={{
        position: 'fixed', left: clampedX, top: clampedY, zIndex: 99999,
        background: 'var(--bg-elevated, #2a2a2a)',
        border: '1px solid var(--border, rgba(255,255,255,0.15))',
        borderRadius: 6, padding: '4px 0',
        boxShadow: '0 4px 12px var(--shadow, rgba(0,0,0,0.5))',
        minWidth: MENU_MIN_W,
      }}
    >
      {items.map((entry, i) => {
        if ('separator' in entry && entry.separator) {
          return <div key={`sep-${i}`} role="separator" style={{ height: 1, background: 'var(--border, rgba(255,255,255,0.15))', margin: '2px 0' }} />
        }
        const item = entry as ContextMenuItem
        return (
          <div
            key={item.action}
            role="menuitem"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); handleAction(item.action) }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                e.stopPropagation()
                handleAction(item.action)
              }
            }}
            style={{
              padding: '6px 16px', fontSize: 12, cursor: 'pointer',
              color: item.danger ? 'var(--danger, #f38ba8)' : 'var(--text, #e0e0e0)',
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.1)' }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {/* Fixed-width slot so labels line up whether or not a row has an
                  icon — a ragged left edge is what an inline emoji produced. */}
              <span style={{
                width: 14, display: 'inline-flex', alignItems: 'center',
                justifyContent: 'center', flexShrink: 0, opacity: 0.85,
              }}>
                {item.icon ? <item.icon size={13} /> : null}
              </span>
              {item.label}
              {item.shortcut ? (
                <span style={{
                  marginLeft: 'auto', paddingLeft: 16, fontSize: 11,
                  color: 'var(--text-muted, #888)', flexShrink: 0,
                }}>{item.shortcut}</span>
              ) : null}
            </span>
          </div>
        )
      })}
    </div>
  )
}
