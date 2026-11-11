/**
 * The canvas's right-click menu: copy and paste, delete, stacking order, lock.
 * A floating sticker in the app's own theme; the row under the pointer or the
 * keyboard is a purple sticker of its own.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { FLOAT_SHADOW } from './canvasPaint'

export type MenuAction =
  | 'copy' | 'paste' | 'delete'
  | 'front' | 'forward' | 'backward' | 'back'
  | 'lock' | 'unlockAll'

interface Item { action: MenuAction; label: string; keys: string; needsSelection?: boolean }

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

interface Props {
  /** Where the menu opens, in the canvas's own px. */
  x: number
  y: number
  /** The canvas's size, so the menu stays inside it. */
  bounds: { w: number; h: number }
  hasSelection: boolean
  /** The selection holds an unlocked shape; Lock is the only item a locked one allows. */
  canEdit: boolean
  canPaste: boolean
  anyLocked: boolean
  onAction: (a: MenuAction) => void
  onClose: () => void
}

export default function CanvasContextMenu({ x, y, bounds, hasSelection, canEdit, canPaste, anyLocked, onAction, onClose }: Props) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ x, y })

  // A Mac shows the modifier symbols; elsewhere a shortcut is spelled in words,
  // which some keyboards print differently (German "Strg" for Ctrl), so each
  // whole shortcut is one catalog string with the key filled in.
  const ctrl = (key: string) => (isMac ? `⌘${key}` : t('components.canvas.shortcut_ctrl', { key }))
  const shiftCtrl = (key: string) => (isMac ? `⇧⌘${key}` : t('components.canvas.shortcut_shift_ctrl', { key }))
  const altShiftCtrl = (key: string) => (isMac ? `⌥⇧⌘${key}` : t('components.canvas.shortcut_alt_shift_ctrl', { key }))

  const groups: Item[][] = [
    [
      { action: 'copy', label: t('components.canvas.menu_copy'), keys: ctrl('C'), needsSelection: true },
      { action: 'paste', label: t('components.canvas.menu_paste'), keys: ctrl('V') },
    ],
    [{ action: 'delete', label: t('components.canvas.menu_delete'), keys: '⌫', needsSelection: true }],
    [
      { action: 'front', label: t('components.canvas.menu_bring_to_front'), keys: ']', needsSelection: true },
      { action: 'forward', label: t('components.canvas.menu_bring_forward'), keys: ctrl(']'), needsSelection: true },
      { action: 'backward', label: t('components.canvas.menu_send_backward'), keys: ctrl('['), needsSelection: true },
      { action: 'back', label: t('components.canvas.menu_send_to_back'), keys: '[', needsSelection: true },
    ],
    [
      { action: 'lock', label: t('components.canvas.menu_lock_unlock'), keys: shiftCtrl('L'), needsSelection: true },
      { action: 'unlockAll', label: t('components.canvas.menu_unlock_all'), keys: altShiftCtrl('L') },
    ],
  ]

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
    if (it.needsSelection && it.action !== 'lock' && !canEdit) return false
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
    <div ref={ref} role="menu" tabIndex={-1} aria-label={t('components.canvas.menu_label')} data-canvas-ui data-testid="canvas-context-menu" onKeyDown={onKeyDown}
      onPointerDown={e => e.stopPropagation()} onContextMenu={e => e.preventDefault()}
      className="absolute w-[240px] rounded-2xl bg-bg-elevated py-1.5 text-text"
      style={{ left: pos.x, top: pos.y, zIndex: 20, fontSize: 13, boxShadow: FLOAT_SHADOW }}>
      {groups.map((group, g) => (
        <div key={g} className={g ? 'mt-1.5 border-t border-border pt-1.5' : undefined}>
          {group.map(it => (
            <button key={it.action} type="button" role="menuitem" disabled={!enabled(it)} onClick={() => { onAction(it.action); onClose() }}
              className="group mx-1.5 flex w-[calc(100%-12px)] items-center justify-between rounded-[10px] px-3 py-1.5 text-left outline-none enabled:hover:bg-accent enabled:hover:text-accent-fg focus-visible:bg-accent focus-visible:text-accent-fg disabled:opacity-40">
              <span>{it.label}</span>
              <span className="text-[12px] text-muted group-enabled:group-hover:text-accent-fg group-focus-visible:text-accent-fg">{it.keys}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}
