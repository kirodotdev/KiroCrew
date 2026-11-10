/**
 * The floating toolbar over selected shapes, after FigJam's: a dark, compact bar
 * with the shape type, the fill and colour, the outline and its weight, and delete.
 */

import { Ban, ChevronDown, Circle, Diamond, Ellipsis, Menu as LinesIcon, Slash, Square, SquareDashed, SquareSlash, StickyNote, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import type { BorderStyle, FillMode, Shape, ShapeKind, StrokeWeight, Swatch } from './canvasModel'
import { STROKE_WIDTH } from './shapeLook'
import SwatchPicker, { PANEL, type PickerMode } from './SwatchPicker'

const SWITCHABLE = new Set<ShapeKind>(['rect', 'ellipse', 'diamond', 'sticky'])

const RING = '#9747FF'
const rule = <span className="mx-1 h-5 w-px" style={{ background: 'rgba(255,255,255,.14)' }} />

/** A small dropdown: an icon button with a chevron that opens a dark list above it. */
function Menu({ label, icon, children }: { label: string; icon: ReactNode; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('pointerdown', away); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div ref={root} className="relative">
      <button type="button" aria-label={label} title={label} aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="flex items-center gap-0.5 rounded-lg px-1.5 py-1 hover:bg-white/10" style={{ color: '#FFFFFF' }}>
        {icon}<ChevronDown size={12} style={{ opacity: 0.7 }} />
      </button>
      {open && (
        <div role="menu" aria-label={label} className="absolute left-0 flex min-w-[160px] flex-col rounded-xl p-1.5 shadow-xl"
          style={{ bottom: '100%', marginBottom: 10, background: PANEL, zIndex: 10 }}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  )
}

function Item({ label, selected, onClick, children }: { label: string; selected: boolean; onClick: () => void; children?: ReactNode }) {
  return (
    <button type="button" role="menuitemradio" aria-checked={selected} aria-label={label} onClick={onClick}
      className="flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-[13px] hover:bg-white/10"
      style={{ color: '#FFFFFF', outline: selected ? `2px solid ${RING}` : 'none', outlineOffset: -2 }}>
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
  onHex: (hex: string, final: boolean) => void
  onBorderHex: (hex: string, final: boolean) => void
  onDelete: () => void
}

export default function ShapeToolbar({ shapes, onColor, onFill, onKind, onWeight, onBorder, onBorderColor, onHex, onBorderHex, onDelete }: Props) {
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
  const same = <T,>(pick: (s: Shape) => T): T | undefined => (shapes.every(s => pick(s) === pick(shapes[0])) ? pick(shapes[0]) : undefined)
  const kind = same(s => s.kind)
  const canSwitch = shapes.every(s => SWITCHABLE.has(s.kind))
  const weight = same(s => s.weight ?? 'medium')
  const hex = same(s => s.hex)
  const borderHex = same(s => s.borderHex)
  const ownBorder = shapes.some(s => s.borderColor || s.borderHex)
  return (
    <div className="flex items-center gap-0.5 rounded-xl px-1.5 py-1 shadow-xl" data-testid="canvas-toolbar" style={{ background: PANEL, color: '#FFFFFF' }}>
      {canSwitch && (
        <Menu label={t('components.canvas.shape')} icon={kinds.find(k => k.kind === kind)?.icon ?? <Square size={16} />}>
          {close => kinds.map(k => (
            <Item key={k.kind} label={k.label} selected={kind === k.kind} onClick={() => { onKind(k.kind); close() }}>{k.icon}</Item>
          ))}
        </Menu>
      )}
      {canSwitch && painted.length > 0 && rule}
      {painted.length > 0 && (
        <SwatchPicker label={t('components.canvas.colour')} show="paper" dark value={hex ? undefined : same(s => s.color)} fallback="#757575"
          custom={{ hex, start: '#3FAAFF', onHex }}
          modes={fillModes} modesLabel={t('components.canvas.fill')} mode={same(s => s.fill ?? 'solid')} onMode={m => onFill(m as FillMode)} ring={same(s => s.fill) === 'none'}
          onPick={onColor} />
      )}
      {painted.length > 0 && rule}
      {painted.length > 0 && (
        <SwatchPicker label={t('components.canvas.border')} show="paper" dark value={borderHex ? undefined : same(s => s.borderColor)} fallback="#757575"
          custom={{ hex: borderHex, start: '#0B78D6', onHex: onBorderHex }}
          icon={<LinesIcon size={18} style={{ color: '#FFFFFF' }} />}
          modes={borderModes} modesLabel={t('components.canvas.border')} mode={same(s => s.border ?? 'solid')} onMode={m => onBorder(m as BorderStyle)}
          onPick={onBorderColor}
          footer={(
            <>
            <button type="button" role="radio" aria-checked={!ownBorder} aria-label={t('components.canvas.match_fill')} onClick={() => onBorderColor(undefined)}
              className="-mx-1 rounded-lg px-2.5 py-1.5 text-left text-[13px]"
              style={{ color: '#FFFFFF', background: !ownBorder ? RING : 'transparent' }}>
              {t('components.canvas.match_fill')} <span style={{ opacity: 0.6 }}>{t('components.canvas.match_fill_hint')}</span>
            </button>
            <div role="radiogroup" aria-label={t('components.canvas.stroke_weight')} className="-mx-1 flex gap-1 border-t pt-2" style={{ borderColor: 'rgba(255,255,255,.12)' }}>
              {weights.map(w => (
                <button key={w.weight} type="button" role="radio" aria-checked={weight === w.weight} aria-label={w.label} onClick={() => onWeight(w.weight)}
                  className="flex flex-1 items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px]"
                  style={{ color: '#FFFFFF', background: weight === w.weight ? RING : 'transparent' }}>
                  <span className="block w-5 rounded-full" style={{ height: STROKE_WIDTH[w.weight], background: '#FFFFFF' }} />{w.label}
                </button>
              ))}
            </div>
            </>
          )} />
      )}
      {painted.length > 0 && rule}
      <button type="button" aria-label={t('components.canvas.delete')} title={t('components.canvas.delete')} onClick={onDelete} className="rounded-lg p-1.5 hover:bg-white/10" style={{ color: 'rgba(255,255,255,.8)' }}>
        <Trash2 size={14} />
      </button>
    </div>
  )
}
