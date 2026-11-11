/**
 * The floating toolbar over selected shapes: a compact sticker in the app's theme
 * with the shape type, the fill and colour, the outline and its weight, and delete.
 */

import { AlignCenter, AlignLeft, AlignRight, Ban, ChevronDown, Circle, Diamond, Ellipsis, Menu as LinesIcon, Slash, Square, SquareDashed, SquareSlash, StickyNote, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { defaultAlign, defaultFontSize, type BorderStyle, type FillMode, type Shape, type ShapeKind, type StrokeWeight, type Swatch, type TextAlign } from './canvasModel'
import { STROKE_WIDTH } from './shapeLook'
import { FLOAT_SHADOW } from './canvasPaint'
import SwatchPicker, { type PickerMode } from './SwatchPicker'
import TextSizeMenu from './TextSizeMenu'

const SWITCHABLE = new Set<ShapeKind>(['rect', 'ellipse', 'diamond', 'sticky'])

export const rule = <span className="mx-1 h-5 w-px bg-border" />
/** A chosen option in a menu or list: a purple sticker, as the dock's active tool is. */
export const picked = (on: boolean) => (on ? 'bg-accent text-accent-fg' : 'hover:bg-bg-hover')

/** A small dropdown: an icon button with a chevron that opens a list above it. */
export function Menu({ label, icon, children }: { label: string; icon: ReactNode; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('pointerdown', away, true); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div ref={root} className="static md:relative">
      <button type="button" aria-label={label} title={label} aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="flex items-center gap-0.5 rounded-lg px-1.5 py-1 hover:bg-bg-hover">
        {icon}<ChevronDown size={12} style={{ opacity: 0.7 }} />
      </button>
      {open && (
        <div role="menu" aria-label={label} className="absolute flex min-w-[160px] flex-col gap-0.5 rounded-2xl bg-bg-elevated p-1.5 text-text inset-x-0 top-full mt-2 md:inset-x-auto md:left-0 md:top-auto md:bottom-full md:mt-0 md:mb-2.5"
          style={{ zIndex: 10, boxShadow: FLOAT_SHADOW }}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  )
}

export function Item({ label, selected, onClick, children }: { label: string; selected: boolean; onClick: () => void; children?: ReactNode }) {
  return (
    <button type="button" role="menuitemradio" aria-checked={selected} aria-label={label} onClick={onClick}
      className={`flex items-center gap-2.5 rounded-[10px] px-2.5 py-1.5 text-left text-[13px] ${picked(selected)}`}>
      {children}<span>{label}</span>
    </button>
  )
}

interface Props {
  shapes: Shape[]
  onColor: (c: Swatch) => void
  onFill: (m: FillMode) => void
  onKind: (k: ShapeKind) => void
  onWeight: (w: StrokeWeight) => void
  onBorder: (b: BorderStyle) => void
  onBorderColor: (c: Swatch | undefined) => void
  /** A free colour for the fill or the border; *final* is false while it is still being dragged or typed. */
  onHex: (hex: string, final: boolean, fresh: boolean) => void
  onBorderHex: (hex: string, final: boolean, fresh: boolean) => void
  onAlign: (a: TextAlign) => void
  onSize: (px: number) => void
  onDelete: () => void
}

export default function ShapeToolbar({ shapes, onColor, onFill, onKind, onWeight, onBorder, onBorderColor, onHex, onBorderHex, onAlign, onSize, onDelete }: Props) {
  const { t } = useTranslation()
  const kinds: { kind: ShapeKind; label: string; icon: ReactNode }[] = [
    { kind: 'rect', label: t('components.canvas.tool_rect'), icon: <Square size={16} /> },
    { kind: 'ellipse', label: t('components.canvas.tool_ellipse'), icon: <Circle size={16} /> },
    { kind: 'diamond', label: t('components.canvas.tool_diamond'), icon: <Diamond size={16} /> },
    { kind: 'sticky', label: t('components.canvas.tool_sticky'), icon: <StickyNote size={16} /> },
  ]
  const weights: { weight: StrokeWeight; label: string }[] = [
    { weight: 'thin', label: t('components.canvas.weight_thin') },
    { weight: 'medium', label: t('components.canvas.weight_medium') },
    { weight: 'thick', label: t('components.canvas.weight_thick') },
  ]
  const fillModes: PickerMode[] = [
    { mode: 'solid', label: t('components.canvas.fill_solid'), icon: <Square size={15} fill="currentColor" /> },
    { mode: 'transparent', label: t('components.canvas.fill_transparent'), icon: <SquareDashed size={15} /> },
    { mode: 'none', label: t('components.canvas.fill_none'), icon: <SquareSlash size={15} /> },
  ]
  const borderModes: PickerMode[] = [
    { mode: 'solid', label: t('components.canvas.border_solid'), icon: <Slash size={15} /> },
    { mode: 'dashed', label: t('components.canvas.border_dashed'), icon: <Ellipsis size={15} /> },
    { mode: 'none', label: t('components.canvas.border_none'), icon: <Ban size={15} /> },
  ]
  const painted = shapes.filter(s => s.kind !== 'text')
  // Text controls show only once a selected shape has text: alignment and size
  // mean nothing on an empty one. Sections are titled, not filled with text.
  const texted = shapes.filter(s => s.kind !== 'section' && s.text.trim())
  const size = texted.length && texted.every(s => (s.fontSize ?? defaultFontSize(s.kind)) === (texted[0].fontSize ?? defaultFontSize(texted[0].kind)))
    ? texted[0].fontSize ?? defaultFontSize(texted[0].kind) : undefined
  const align = texted.length && texted.every(s => (s.align ?? defaultAlign(s.kind)) === (texted[0].align ?? defaultAlign(texted[0].kind)))
    ? texted[0].align ?? defaultAlign(texted[0].kind) : undefined
  const aligns: { align: TextAlign; label: string; icon: ReactNode }[] = [
    { align: 'left', label: t('components.canvas.align_left'), icon: <AlignLeft size={16} /> },
    { align: 'center', label: t('components.canvas.align_center'), icon: <AlignCenter size={16} /> },
    { align: 'right', label: t('components.canvas.align_right'), icon: <AlignRight size={16} /> },
  ]
  const same = <T,>(pick: (s: Shape) => T): T | undefined => (shapes.every(s => pick(s) === pick(shapes[0])) ? pick(shapes[0]) : undefined)
  const kind = same(s => s.kind)
  const canSwitch = shapes.every(s => SWITCHABLE.has(s.kind))
  const weight = same(s => s.weight ?? 'medium')
  const hex = same(s => s.hex)
  const borderHex = same(s => s.borderHex)
  const ownBorder = shapes.some(s => s.borderColor || s.borderHex)
  return (
    <div className="flex flex-wrap items-center justify-center gap-0.5 rounded-2xl bg-bg-elevated px-1.5 py-1 text-text md:flex-nowrap md:justify-start" data-testid="canvas-toolbar" style={{ boxShadow: FLOAT_SHADOW }}>
      {canSwitch && (
        <Menu label={t('components.canvas.shape')} icon={kinds.find(k => k.kind === kind)?.icon ?? <Square size={16} />}>
          {close => kinds.map(k => (
            <Item key={k.kind} label={k.label} selected={kind === k.kind} onClick={() => { onKind(k.kind); close() }}>{k.icon}</Item>
          ))}
        </Menu>
      )}
      {canSwitch && painted.length > 0 && rule}
      {painted.length > 0 && (
        <SwatchPicker label={t('components.canvas.colour')} show="paper" value={hex ? undefined : same(s => s.color)} fallback="#757575"
          custom={{ hex, start: '#3FAAFF', onHex }}
          modes={fillModes} modesLabel={t('components.canvas.fill')} mode={same(s => s.fill ?? 'solid')} onMode={m => onFill(m as FillMode)} ring={same(s => s.fill) === 'none'}
          onPick={onColor} />
      )}
      {painted.length > 0 && rule}
      {painted.some(s => s.kind !== 'sticky') && (
        <SwatchPicker label={t('components.canvas.border')} show="paper" value={borderHex ? undefined : same(s => s.borderColor)} fallback="#757575"
          custom={{ hex: borderHex, start: '#0B78D6', onHex: onBorderHex }}
          icon={<LinesIcon size={18} />}
          modes={borderModes} modesLabel={t('components.canvas.border')} mode={same(s => s.border ?? 'solid')} onMode={m => onBorder(m as BorderStyle)}
          onPick={onBorderColor}
          footer={(
            <>
            <button type="button" role="radio" aria-checked={!ownBorder} aria-label={t('components.canvas.edge_white')} onClick={() => onBorderColor(undefined)}
              className={`-mx-1 rounded-[10px] px-2.5 py-1.5 text-left text-[13px] ${picked(!ownBorder)}`}>
              {t('components.canvas.edge_white')}
            </button>
            <div role="radiogroup" aria-label={t('components.canvas.stroke_weight')} className="-mx-1 flex gap-1 border-t border-border pt-2">
              {weights.map(w => (
                <button key={w.weight} type="button" role="radio" aria-checked={weight === w.weight} aria-label={w.label} onClick={() => onWeight(w.weight)}
                  className={`flex flex-1 items-center gap-2 rounded-[10px] px-2.5 py-1.5 text-[13px] ${picked(weight === w.weight)}`}>
                  <span className="block w-5 rounded-full bg-current" style={{ height: STROKE_WIDTH[w.weight] }} />{w.label}
                </button>
              ))}
            </div>
            </>
          )} />
      )}
      {painted.some(s => s.kind !== 'sticky') && rule}
      {texted.length > 0 && (
        <>
          <Menu label={t('components.canvas.align')} icon={aligns.find(a => a.align === align)?.icon ?? <AlignCenter size={16} />}>
            {close => aligns.map(a => (
              <Item key={a.align} label={a.label} selected={align === a.align} onClick={() => { onAlign(a.align); close() }}>{a.icon}</Item>
            ))}
          </Menu>
          <TextSizeMenu size={size} onSize={onSize} />
          {rule}
        </>
      )}
      <button type="button" aria-label={t('components.canvas.delete')} title={t('components.canvas.delete')} onClick={onDelete} className="rounded-lg p-1.5 text-muted hover:bg-bg-hover hover:text-text">
        <Trash2 size={14} />
      </button>
    </div>
  )
}
