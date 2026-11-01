/**
 * The whiteboard's right-click menu (PoC), after FigJam's: copy and paste,
 * delete, stacking order, lock. Dark in every theme, like FigJam's. Labels are
 * hardcoded English like the rest of the board canvas.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export type MenuAction =
  | 'copy' | 'paste' | 'delete'
  | 'front' | 'forward' | 'backward' | 'back'
  | 'lock' | 'unlockAll'

interface Item { action: MenuAction; label: string; keys: string; needsSelection?: boolean }

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
const mod = isMac ? '⌘' : 'Ctrl+'
const shift = isMac ? '⇧' : 'Shift+'
const alt = isMac ? '⌥' : 'Alt+'

const GROUPS: Item[][] = [
  [
    { action: 'copy', label: 'Copy', keys: `${mod}C`, needsSelection: true },
    { action: 'paste', label: 'Paste', keys: `${mod}V` },
  ],
  [{ action: 'delete', label: 'Delete', keys: '⌫', needsSelection: true }],
  [
    { action: 'front', label: 'Bring to front', keys: ']', needsSelection: true },
    { action: 'forward', label: 'Bring forward', keys: `${mod}]`, needsSelection: true },
    { action: 'backward', label: 'Send backward', keys: `${mod}[`, needsSelection: true },
    { action: 'back', label: 'Send to back', keys: '[', needsSelection: true },
  ],
  [
    { action: 'lock', label: 'Lock/Unlock', keys: `${shift}${mod}L`, needsSelection: true },
    { action: 'unlockAll', label: 'Unlock all objects', keys: `${alt}${shift}${mod}L` },
  ],
]

// FigJam's menu is dark in every theme, so these are fixed rather than tokens.
const PANEL = '#1E1E1E'
const HOVER = '#0C8CE9'
const RULE = 'rgba(255,255,255,.12)'

interface Props {
  /** Where the menu opens, in the canvas's own px. */
  x: number
  y: number
  /** The canvas's size, so the menu stays inside it. */
  bounds: { w: number; h: number }
  hasSelection: boolean
  canPaste: boolean
  anyLocked: boolean
  onAction: (a: MenuAction) => void
  onClose: () => void
}

export default function BoardContextMenu({ x, y, bounds, hasSelection, canPaste, anyLocked, onAction, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ x, y })

  // Flip in from the edges when the menu would overflow the canvas.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    setPos({ x: x + w > bounds.w ? Math.max(4, x - w) : x, y: y + h > bounds.h ? Math.max(4, bounds.h - h - 4) : y })
  }, [x, y, bounds.w, bounds.h])

  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    const away = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) onClose() }
    document.addEventListener('pointerdown', away, true)
    return () => document.removeEventListener('pointerdown', away, true)
  }, [onClose])

  const enabled = (it: Item) => {
    if (it.needsSelection && !hasSelection) return false
    if (it.action === 'paste') return canPaste
    if (it.action === 'unlockAll') return anyLocked
    return true
  }

  // Arrow keys move between the items, as in a native menu; Escape closes it.
  const onKeyDown = (e: React.KeyboardEvent) => {
    e.stopPropagation()
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const items = [...(ref.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
    const i = items.indexOf(document.activeElement as HTMLButtonElement)
    items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus()
  }

  return (
    <div ref={ref} role="menu" tabIndex={-1} aria-label="Board actions" data-board-ui data-testid="board-context-menu" onKeyDown={onKeyDown}
      onPointerDown={e => e.stopPropagation()} onContextMenu={e => e.preventDefault()}
      className="absolute w-[240px] rounded-xl py-1.5 shadow-2xl"
      style={{ left: pos.x, top: pos.y, background: PANEL, color: '#FFFFFF', zIndex: 20, fontSize: 13 }}>
      {GROUPS.map((group, g) => (
        <div key={g} style={g ? { borderTop: `1px solid ${RULE}`, marginTop: 6, paddingTop: 6 } : undefined}>
          {group.map(it => (
            <button key={it.action} type="button" role="menuitem" disabled={!enabled(it)} onClick={() => { onAction(it.action); onClose() }}
              className="flex w-full items-center justify-between rounded-md px-3 py-1.5 text-left outline-none disabled:opacity-40"
              style={{ width: 'calc(100% - 12px)', margin: '0 6px' }}
              onMouseEnter={e => { if (!e.currentTarget.disabled) e.currentTarget.style.background = HOVER }}
              onMouseLeave={e => { e.currentTarget.style.background = '' }}
              onFocus={e => { e.currentTarget.style.background = HOVER }}
              onBlur={e => { e.currentTarget.style.background = '' }}>
              <span>{it.label}</span>
              <span style={{ opacity: 0.6, fontSize: 12 }}>{it.keys}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}
