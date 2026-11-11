/**
 * The text size picker shared by the text and shape toolbars: a button naming
 * the size, opening FigJam's named steps and a field for any other size.
 */
import { ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useImeGuard } from '../../hooks/useImeGuard'

import { MAX_TEXT_SIZE, MIN_TEXT_SIZE, TEXT_SIZES, type TextSizeName } from './canvasModel'
import { FLOAT_SHADOW } from './canvasPaint'
import { picked } from './ShapeToolbar'

/** The size picker: a button naming the size, opening the named steps and a number field. */
export default function TextSizeMenu({ size, onSize }: { size: number | undefined; onSize: (px: number) => void }) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const root = useRef<HTMLDivElement>(null)
  const names: Record<TextSizeName, string> = {
    small: t('components.canvas.size_small'),
    medium: t('components.canvas.size_medium'),
    large: t('components.canvas.size_large'),
    xlarge: t('components.canvas.size_xlarge'),
    huge: t('components.canvas.size_huge'),
  }
  const named = (Object.keys(TEXT_SIZES) as TextSizeName[]).find(n => TEXT_SIZES[n] === size)
  const label = size === undefined ? t('components.canvas.size_mixed') : named ? names[named] : t('components.canvas.size_custom', { size })
  useEffect(() => {
    if (!open) return
    setDraft(size === undefined ? '' : String(size))
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('pointerdown', away, true); document.removeEventListener('keydown', esc) }
  }, [open, size])
  const applyDraft = () => {
    const n = Number(draft)
    if (Number.isFinite(n) && n >= MIN_TEXT_SIZE && n <= MAX_TEXT_SIZE) onSize(n)
  }
  return (
    <div ref={root} className="static md:relative">
      <button type="button" aria-label={t('components.canvas.text_size')} title={t('components.canvas.text_size')} aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="flex min-w-[112px] items-center justify-between gap-2 rounded-lg bg-bg-hover px-2.5 py-1 text-[13px] hover:bg-bg-hover">
        <span>{label}</span><ChevronDown size={12} className="text-muted" />
      </button>
      {open && (
        <div role="menu" aria-label={t('components.canvas.text_size')} className="absolute inset-x-0 top-full mt-2 flex min-w-[180px] flex-col gap-0.5 rounded-2xl bg-bg-elevated p-1.5 text-text md:inset-x-auto md:left-0 md:mt-2.5"
          style={{ zIndex: 10, boxShadow: FLOAT_SHADOW }}>
          {(Object.keys(TEXT_SIZES) as TextSizeName[]).map(n => (
            // Each step is drawn at a size that hints at it, capped so the list stays compact.
            <button key={n} type="button" role="menuitemradio" aria-checked={named === n} onClick={() => { onSize(TEXT_SIZES[n]); setOpen(false) }}
              className={`rounded-[10px] px-3 py-1.5 text-left ${picked(named === n)}`} style={{ fontSize: Math.min(26, 12 + TEXT_SIZES[n] / 4) }}>
              {names[n]}
            </button>
          ))}
          <div className="mt-1 border-t border-border pt-1.5">
            <input aria-label={t('components.canvas.text_size_custom')} inputMode="numeric" value={draft}
              onChange={e => setDraft(e.target.value.replace(/[^\d]/g, ''))}
              {...ime.bindComposition<HTMLInputElement>({ onBlur: applyDraft })}
              onKeyDown={e => { e.stopPropagation(); if (e.key === 'Enter' && ime.claimEnter(e)) { applyDraft(); setOpen(false) } }}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[13px] text-text outline-none focus:border-accent" />
          </div>
        </div>
      )}
    </div>
  )
}
