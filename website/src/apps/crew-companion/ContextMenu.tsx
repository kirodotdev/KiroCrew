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
      style={{
        position: 'fixed', left: clampedX, top: clampedY, zIndex: 99999,
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        borderRadius: 6, padding: '4px 0',
        boxShadow: '0 4px 12px var(--shadow, rgba(0,0,0,0.5))',
        minWidth: MENU_MIN_W,
      }}
    >
      {items.map((entry, i) => {
        if ('separator' in entry && entry.separator) {
          return <div key={`sep-${i}`} style={{ height: 1, background: 'var(--border)', margin: '2px 0' }} />
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
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-hover)' }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
          >
            {item.label}
          </div>
        )
      })}
    </div>
  )
}
