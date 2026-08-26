/**
 * Reusable context menu component.
 * Used by PetWidget (overlay) and ChatPanel (chat window).
 * Handles edge clamping, click-outside dismiss, and optional hitbox reporting for overlay use.
 */
import React, { useEffect, useRef, useCallback } from 'react'

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
