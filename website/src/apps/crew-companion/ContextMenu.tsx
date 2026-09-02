import React from 'react'
/**
 * Reusable context menu component.
 * Used by PetWidget (overlay) and ChatPanel (chat window).
 * Handles edge clamping, click-outside dismiss, and optional hitbox reporting for overlay use.
 */
import { useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import { useMenuKeyboard } from '../../hooks/useMenuKeyboard'
import { petBridge } from './petBridge'

const api = petBridge

export interface ContextMenuItem {
  label: string
  action: string
  danger?: boolean
  separator?: false
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
   * Shared role="menu" keyboard contract (#6231, ported here for #6266): arrow
   * navigation with wrap, Home/End, Tab containment, and focus entry onto the
   * first row on open. This copy was absent from #6231's five-surface inventory
   * because the inventory enumerated `role="menu"` containers — which this file
   * lacked by omission — so its rows carried orphaned `role="menuitem"` with no
   * owning menu and no keyboard contract behind the promise that role makes.
   *
   * The component renders unconditionally while mounted (hosts unmount it to
   * close), so `enabled` is simply true and the hook's cleanup drops the
   * document listener on unmount — same posture as the mochi sibling.
   *
   * Escape and Enter/Space stay where they are: the hook deliberately owns
   * navigation only. The window-level Escape/click-outside/blur closers below
   * are independent of the hook and unchanged.
   */
  useMenuKeyboard({ enabled: true, containerRef: menuRef })

  /*
   * The element that was focused when this menu MOUNTED — the surface the user
   * right-clicked (or reached with the keyboard). Captured during the first
   * RENDER on purpose, not in an effect: render runs before `useMenuKeyboard`'s
   * focus-entry effect moves focus onto the first row, so this is the last
   * moment the opener is still `document.activeElement`. `undefined` is the
   * "not yet captured" sentinel — `activeElement` itself can in principle be
   * null, and using null for both would re-run the capture on a later render
   * and latch a menu ROW as the opener. Same spelling as the mochi sibling.
   *
   * In the overlay host today the opener is `<body>` — the `.cc-pet` div the
   * user right-clicks is not focusable — so the restore below is a no-op
   * there. The capture exists for hosts whose opener IS focusable (and for a
   * future focusable pet); making `.cc-pet` focusable is its own change.
   */
  const opener = useRef<Element | null | undefined>(undefined)
  if (opener.current === undefined) opener.current = document.activeElement

  /*
   * Give focus back when the menu goes away. Focus ENTRY without a matching
   * restore is a regression, not half a feature: hosts render this component
   * conditionally, so closing it UNMOUNTS the element that holds focus and the
   * browser drops focus to `<body>`. Restore only if focus is still inside the
   * menu being destroyed — if an action moved focus elsewhere, or an outside
   * click already did, focus has a rightful owner and is left alone.
   *
   * `useLayoutEffect`, NOT `useEffect`: layout cleanups run synchronously while
   * the menu is STILL in the document and still holds focus; a passive cleanup
   * runs after the commit, when `activeElement` has already reset to `<body>`,
   * so the guard would read "focus is not in the menu" every time and restore
   * nothing. The container is captured at mount rather than read as
   * `menuRef.current` in the cleanup because React detaches host refs while
   * tearing the tree down.
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

  // Report the menu's interactive region to the main process (overlay only).
  //
  // The overlay window is click-through everywhere EXCEPT the rects it reports: the
  // main process polls the cursor and only lets the page accept a click when the
  // cursor is inside one of them. While the menu is open we report the WHOLE
  // viewport, not just the menu's own box — a menu is a modal moment, so it is
  // legitimate for the overlay to capture every click until it closes. Reporting
  // only the menu box (the previous behaviour) meant a click just OUTSIDE it was
  // forwarded to the desktop and never reached this page, so the close-on-outside
  // listener below could never fire and the menu could not be dismissed by clicking
  // away. The cleanup clears the rect the instant the menu closes, so the overlay
  // returns to click-through and no stale full-screen hitbox is left capturing the
  // user's screen.
  useEffect(() => {
    if (!reportHitbox) return
    api?.setMenuHitbox?.({ x: 0, y: 0, w: window.innerWidth, h: window.innerHeight })
    return () => { api?.setMenuHitbox?.(null) }
  }, [reportHitbox])

  // Close on click outside, Escape, or window losing focus.
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose()
    }
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const handleBlur = () => onClose()

    // Deferred one tick so the click that OPENED the menu does not immediately
    // close it again.
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
    }
  }, [onClose])

  const handleAction = useCallback((action: string) => {
    onClose()
    onAction(action)
  }, [onClose, onAction])

  return (
    <>
      {reportHitbox ? (
        <div
          className="cc-menu-backdrop"
          role="presentation"
          onMouseDown={onClose}
          style={{
            // A transparent, full-viewport catcher that sits just UNDER the menu.
            //
            // The overlay's html and body are `pointer-events: none`, so a click on
            // an empty region of the desktop hits no element and dispatches no DOM
            // event — the window-level "click outside closes me" listener could never
            // fire there. This backdrop is a real element under the cursor, so an
            // outside click lands on it and dismisses the menu. Paired with the
            // full-viewport hitbox above (which is what makes the overlay accept the
            // click at all), it is what restores click-anywhere-to-dismiss. Rendered
            // only in the overlay (`reportHitbox`); the chat window's menu keeps its
            // ordinary click-outside behaviour untouched.
            position: 'fixed',
            inset: 0,
            zIndex: 99998,
            pointerEvents: 'auto',
            background: 'transparent',
          }}
        />
      ) : null}
      <div
        ref={menuRef}
        role="menu"
        style={{
          position: 'fixed', left: clampedX, top: clampedY, zIndex: 99999,
        /*
         * Every themed colour here carries a fallback, and that is load-bearing.
         *
         * This menu renders in the pet's OVERLAY window, which has no stylesheet of
         * its own — `adoptDashboardTheme` injects the dashboard's, so the variables
         * arrive late and, if that injection does not take, never. A `var(--x)` with
         * no fallback is then an INVALID value, which for `background` means fully
         * transparent: the menu became the desktop with text floating on it. The
         * giveaway was that `--text` and `--danger` rendered fine here while these
         * three did not — the difference was only the fallback.
         *
         * The values match the app this was ported from, which never dropped them.
         */
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
            role="menuitem" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); handleAction(item.action) } }}
            key={item.action}
            onClick={(e) => { e.stopPropagation(); handleAction(item.action) }}
            style={{
              padding: '6px 16px', fontSize: 12, cursor: 'pointer',
              color: item.danger ? 'var(--danger, #f38ba8)' : 'var(--text, #e0e0e0)',
            }}
            /*
             * Hover uses a theme variable, not a white overlay. The desktop app could
             * assume a dark menu, so `rgba(255,255,255,0.1)` read as a highlight there;
             * on the light theme it is white on white and the hover state vanished.
             */
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-hover, rgba(255,255,255,0.1))' }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
          >
            {item.label}
          </div>
        )
      })}
      </div>
    </>
  )
}
