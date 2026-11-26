/**
 * The floating toolbar over selected text boxes, after FigJam's but kept small:
 * text colour, size (named steps or a number), bold, strikethrough, alignment and
 * delete. The same sticker chrome as the shape toolbar.
 */

import { AlignCenter, AlignLeft, AlignRight, Trash2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { defaultFontSize, type EmphasisStyle, type Shape, type Swatch, type TextAlign } from './canvasModel'
import { FLOAT_SHADOW } from './canvasPaint'
import { EmphasisToggles, Menu, Item, rule } from './ShapeToolbar'
import SwatchPicker from './SwatchPicker'
import TextSizeMenu from './TextSizeMenu'

interface Props {
  shapes: Shape[]
  onInk: (c: Swatch | undefined) => void
  /** A free text colour; *final* is false while it is still being dragged or typed. */
  onInkHex: (hex: string, final: boolean, fresh: boolean) => void
  onSize: (px: number) => void
  onEmphasis: (style: EmphasisStyle, on: boolean) => void
  onAlign: (a: TextAlign) => void
  onDelete: () => void
}

export default function TextToolbar({ shapes, onInk, onInkHex, onSize, onEmphasis, onAlign, onDelete }: Props) {
  const { t } = useTranslation()
  const same = <T,>(pick: (s: Shape) => T): T | undefined => (shapes.every(s => pick(s) === pick(shapes[0])) ? pick(shapes[0]) : undefined)
  const align = same(s => s.align ?? 'left')
  const inkHex = same(s => s.inkHex)
  // As in ShapeToolbar: a shared palette colour needs every shape free of a custom one.
  const anyInkHex = shapes.some(s => s.inkHex)
  const aligns: { align: TextAlign; label: string; icon: ReactNode }[] = [
    { align: 'left', label: t('components.canvas.align_left'), icon: <AlignLeft size={16} /> },
    { align: 'center', label: t('components.canvas.align_center'), icon: <AlignCenter size={16} /> },
    { align: 'right', label: t('components.canvas.align_right'), icon: <AlignRight size={16} /> },
  ]
  return (
    <div className="flex flex-wrap items-center justify-center gap-0.5 rounded-2xl bg-bg-elevated px-1.5 py-1 text-text md:flex-nowrap md:justify-start" data-testid="canvas-text-toolbar" style={{ boxShadow: FLOAT_SHADOW }}>
      <SwatchPicker label={t('components.canvas.text_colour')} show="paper" value={anyInkHex ? undefined : same(s => s.ink)} fallback="var(--text)"
        custom={{ hex: inkHex, start: '#1E1E1E', onHex: onInkHex }}
        onPick={onInk} onReset={() => onInk(undefined)} />
      {rule}
      <TextSizeMenu size={same(s => s.fontSize ?? defaultFontSize(s.kind))} onSize={onSize} />
      {rule}
      <EmphasisToggles shapes={shapes} onEmphasis={onEmphasis} />
      {rule}
      <Menu label={t('components.canvas.align')} icon={aligns.find(a => a.align === align)?.icon ?? <AlignLeft size={16} />}>
        {close => aligns.map(a => (
          <Item key={a.align} label={a.label} selected={align === a.align} onClick={() => { onAlign(a.align); close() }}>{a.icon}</Item>
        ))}
      </Menu>
      {rule}
      <button type="button" aria-label={t('components.canvas.delete')} title={t('components.canvas.delete')} onClick={onDelete} className="rounded-lg p-1.5 text-muted hover:bg-bg-hover hover:text-text">
        <Trash2 size={14} />
      </button>
    </div>
  )
}
