import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'
import { EyeOff, Ghost, Undo2, VenetianMask } from 'lucide-react'

import { i18nT } from '../i18n/t'

export type MemoryMode = 'persistent' | 'incognito' | 'temporary'

interface MemoryModeChipProps {
  memoryMode?: string
  onSwitchMode: (mode: MemoryMode) => void
}

const GAP = 6
const EDGE = 8

/** Memory-mode chooser chip: opens an incognito/temporary popover, or offers the way back when one is active. */
export function MemoryModeChip({ memoryMode, onSwitchMode }: MemoryModeChipProps) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ bottom: number; left: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || btnRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      btnRef.current?.focus()
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  // Opens UPWARD (never over the composer): bottom edge GAP above the chip, centred and clamped to the viewport.
  useLayoutEffect(() => {
    if (!open) { setPos(null); return }
    const b = btnRef.current?.getBoundingClientRect()
    const p = popRef.current
    if (!b || !p) return
    const w = p.offsetWidth
    const vw = window.innerWidth
    const bottom = window.innerHeight - b.top + GAP
    const left = Math.min(Math.max(EDGE, b.left + b.width / 2 - w / 2), Math.max(EDGE, vw - EDGE - w))
    setPos({ bottom, left })
  }, [open])

  const currentMode = (memoryMode ?? 'persistent') as MemoryMode
  // A non-persistent memory mode is the ephemeral state; the trigger
  // then offers the way back instead of the chooser.
  const ephemeralActive = currentMode !== 'persistent'
  const label = !ephemeralActive
    ? i18nT('components.welcomeView.choose_memory_mode')
    : currentMode === 'incognito'
      ? i18nT('components.welcomeView.incognito_active_switch_to_persistent')
      : i18nT('components.welcomeView.temporary_active_switch_to_persistent')

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        data-testid="memory-mode-chip"
        className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1 text-[12px] transition-colors cursor-pointer ${
          ephemeralActive
            ? 'border-warn bg-warn-subtle text-warn'
            : 'border-border bg-card text-muted hover:border-accent hover:text-text'
        }`}
        onClick={() => {
          if (!ephemeralActive) setOpen(!open)
          else onSwitchMode('persistent')
        }}
      >
        {!ephemeralActive ? <Ghost size={13} /> : <Undo2 size={13} />}
        <span>{label}</span>
      </button>
      {open && createPortal(
        <div
          ref={popRef}
          data-testid="memory-mode-popover"
          className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl p-2 flex flex-col sm:flex-row gap-2 max-w-[calc(100vw-16px)]"
          // Hidden for the one layout pass before it is measured, so it never flashes at 0,0.
          style={pos ? { bottom: pos.bottom, left: pos.left } : { bottom: 0, left: 0, visibility: 'hidden' }}
        >
          {([
            { key: 'incognito' as const, Icon: EyeOff, label: i18nT('components.welcomeView.incognito'), desc: i18nT('components.welcomeView.incognito_desc'), tile: 'bg-warn-subtle text-warn' },
            { key: 'temporary' as const, Icon: VenetianMask, label: i18nT('components.welcomeView.temporary'), desc: i18nT('components.welcomeView.temporary_desc'), tile: 'bg-aim-subtle text-aim' },
          ] as const).map(t => (
            <button
              key={t.key}
              type="button"
              className="w-[240px] max-w-full min-w-0 p-3 rounded-xl border border-border bg-card hover:border-accent transition-colors text-left grid grid-cols-[30px_1fr] gap-x-2.5 gap-y-1 items-start cursor-pointer"
              onClick={() => { onSwitchMode(t.key); setOpen(false) }}
            >
              <span aria-hidden="true" className={`row-span-2 w-[30px] h-[30px] rounded-lg flex items-center justify-center ${t.tile}`}>
                <t.Icon size={15} />
              </span>
              <span className="text-[13px] font-semibold text-text min-w-0">{t.label}</span>
              <span className="text-[11px] text-muted leading-snug min-w-0">{t.desc}</span>
            </button>
          ))}
        </div>,
        document.body
      )}
    </>
  )
}
