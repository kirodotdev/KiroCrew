import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { OverlayPortal } from '../../components/overlayRoot'
import { i18nT } from '../../i18n/t'

/**
 * Inline rename for one rail row, laid over the row it renames. The name is
 * the rail's own (`RailLayout.labels`): the session list and the roster keep
 * theirs. Enter or blur saves, Escape cancels, and saving an empty name goes
 * back to the live one. Collapsed, the 68px rail cannot hold a field, so it
 * opens to the right of the row instead.
 */
export function RailRenameInput({ initial, collapsed, onSave, onCancel }: {
  initial: string
  collapsed: boolean
  onSave: (name: string | null) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const ref = useRef<HTMLInputElement>(null)
  // A context-menu close returns focus to the trigger after this mounts, so
  // focus on the next frame rather than in the effect's own tick.
  useEffect(() => {
    const raf = requestAnimationFrame(() => { ref.current?.focus(); ref.current?.select() })
    return () => cancelAnimationFrame(raf)
  }, [])
  const done = useRef(false)
  const finish = (save: boolean) => {
    if (done.current) return
    done.current = true
    // An untouched name is not a rename: storing it would pin today's live
    // name and stop the row following later renames elsewhere.
    if (save && value.trim() !== initial.trim()) onSave(value.trim() || null)
    else onCancel()
  }
  // Collapsed: anchor on the row, but draw the field in the overlay root,
  // since the rail's scroller clips anything that overflows it sideways.
  const anchor = useRef<HTMLSpanElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  useLayoutEffect(() => {
    if (!collapsed) return
    const r = anchor.current?.getBoundingClientRect()
    if (r) setPos({ top: r.top + r.height / 2, left: r.right + 8 })
  }, [collapsed])
  const field = (
    <input
      ref={ref}
      value={value}
      onChange={e => setValue(e.target.value)}
      onKeyDown={e => {
        // Keep the row's own keyboard handling (and dnd-kit's) out of typing.
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); finish(true) }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false) }
      }}
      onPointerDown={e => e.stopPropagation()}
      onBlur={() => finish(true)}
      aria-label={i18nT('nav.rail_rename')}
      maxLength={80}
      className={`z-[9999] h-8 rounded-md border border-accent bg-bg-elevated text-text text-sm px-2 outline-none shadow-sm -translate-y-1/2 ${
        collapsed ? 'fixed w-48' : 'absolute inset-x-0 top-1/2'
      }`}
      style={collapsed && pos ? { top: pos.top, left: pos.left } : undefined}
    />
  )
  if (!collapsed) return field
  return (
    <>
      <span ref={anchor} aria-hidden="true" className="absolute inset-0 pointer-events-none" />
      {pos && <OverlayPortal>{field}</OverlayPortal>}
    </>
  )
}
