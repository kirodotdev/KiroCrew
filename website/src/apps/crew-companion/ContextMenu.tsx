import React from 'react'
/**
 * Reusable context menu component.
 * Used by PetWidget (overlay) and ChatPanel (chat window).
 * Handles edge clamping, click-outside dismiss, and optional hitbox reporting for overlay use.
 */
import { useEffect, useRef, useCallback } from 'react'
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
          return <div key={`sep-${i}`} style={{ height: 1, background: 'var(--border, rgba(255,255,255,0.15))', margin: '2px 0' }} />
        }
        const item = entry as ContextMenuItem
        return (
          <div
            role="menuitem" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleAction(item.action) } }}
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
