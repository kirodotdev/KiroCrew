/**
 * The floating toolbar over selected shapes (PoC), after FigJam's: a dark, compact
 * bar with the shape type, the fill and colour, and the outline weight, then the
 * ask-the-conductor and delete actions. Labels are hardcoded English like the rest
 * of the board canvas.
 */

import { ArrowUpRight, Ban, ChevronDown, Circle, Diamond, Ellipsis, Menu as LinesIcon, Shield, ShieldCheck, Slash, Sparkles, Square, SquareDashed, SquareSlash, StickyNote, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'

import type { BorderStyle, ConductorAccess, FillMode, SectionGoal, Shape, ShapeKind, StrokeWeight, Swatch } from './boardModel'
import GoalPopover from './GoalPopover'
import { STROKE_WIDTH } from './shapeLook'
import SwatchPicker, { PANEL, type PickerMode } from './SwatchPicker'

const KINDS: { kind: ShapeKind; label: string; icon: ReactNode }[] = [
  { kind: 'rect', label: 'Rectangle', icon: <Square size={16} /> },
  { kind: 'ellipse', label: 'Ellipse', icon: <Circle size={16} /> },
  { kind: 'diamond', label: 'Diamond', icon: <Diamond size={16} /> },
  { kind: 'sticky', label: 'Sticky note', icon: <StickyNote size={16} /> },
]
const SWITCHABLE = new Set<ShapeKind>(KINDS.map(k => k.kind))

const WEIGHTS: { weight: StrokeWeight; label: string }[] = [
  { weight: 'thin', label: 'Thin' },
  { weight: 'medium', label: 'Medium' },
  { weight: 'thick', label: 'Thick' },
]

const FILL_MODES: PickerMode[] = [
  { mode: 'solid', label: 'Fill', icon: <Square size={15} fill="currentColor" /> },
  { mode: 'transparent', label: 'Transparent', icon: <SquareDashed size={15} /> },
  { mode: 'none', label: 'No fill', icon: <SquareSlash size={15} /> },
]
const BORDER_MODES: PickerMode[] = [
  { mode: 'solid', label: 'Solid', icon: <Slash size={15} /> },
  { mode: 'dashed', label: 'Dashed', icon: <Ellipsis size={15} /> },
  { mode: 'none', label: 'None', icon: <Ban size={15} /> },
]

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
  /** Set or clear the goal of the one selected section. */
  onGoal: (g: SectionGoal | undefined) => void
  /** Whether the conductor may change the one selected section, and the switch for it. */
  access: ConductorAccess
  onAccess: () => void
  onAsk: () => void
  onDelete: () => void
  /** Open the session the one selected session card stands for. */
  onOpen?: () => void
}

export default function ShapeToolbar({ shapes, onColor, onFill, onKind, onWeight, onBorder, onBorderColor, onHex, onBorderHex, onGoal, access, onAccess, onAsk, onDelete, onOpen }: Props) {
  // A session card draws its own look, so it takes no colour or border.
  const painted = shapes.filter(s => s.kind !== 'text' && s.kind !== 'session')
  const card = shapes.length === 1 && shapes[0].kind === 'session' ? shapes[0] : null
  const same = <T,>(pick: (s: Shape) => T): T | undefined => (shapes.every(s => pick(s) === pick(shapes[0])) ? pick(shapes[0]) : undefined)
  const kind = same(s => s.kind)
  const canSwitch = shapes.every(s => SWITCHABLE.has(s.kind))
  const weight = same(s => s.weight ?? 'medium')
  const hex = same(s => s.hex)
  const borderHex = same(s => s.borderHex)
  const ownBorder = shapes.some(s => s.borderColor || s.borderHex)
  return (
    <div className="flex items-center gap-0.5 rounded-xl px-1.5 py-1 shadow-xl" data-testid="board-toolbar" style={{ background: PANEL, color: '#FFFFFF' }}>
      {canSwitch && (
        <Menu label="Shape" icon={KINDS.find(k => k.kind === kind)?.icon ?? <Square size={16} />}>
          {close => KINDS.map(k => (
            <Item key={k.kind} label={k.label} selected={kind === k.kind} onClick={() => { onKind(k.kind); close() }}>{k.icon}</Item>
          ))}
        </Menu>
      )}
      {canSwitch && painted.length > 0 && rule}
      {painted.length > 0 && (
        <SwatchPicker label="Colour" show="paper" dark value={hex ? undefined : same(s => s.color)} fallback="#757575"
          custom={{ hex, start: '#3FAAFF', onHex }}
          modes={FILL_MODES} modesLabel="Fill" mode={same(s => s.fill ?? 'solid')} onMode={m => onFill(m as FillMode)} ring={same(s => s.fill) === 'none'}
          onPick={onColor} />
      )}
      {painted.length > 0 && rule}
      {painted.length > 0 && (
        <SwatchPicker label="Border" show="paper" dark value={borderHex ? undefined : same(s => s.borderColor)} fallback="#757575"
          custom={{ hex: borderHex, start: '#0B78D6', onHex: onBorderHex }}
          icon={<LinesIcon size={18} style={{ color: '#FFFFFF' }} />}
          modes={BORDER_MODES} modesLabel="Border" mode={same(s => s.border ?? 'solid')} onMode={m => onBorder(m as BorderStyle)}
          onPick={onBorderColor}
          footer={(
            <>
            <button type="button" role="radio" aria-checked={!ownBorder} aria-label="Match fill" onClick={() => onBorderColor(undefined)}
              className="-mx-1 rounded-lg px-2.5 py-1.5 text-left text-[13px]"
              style={{ color: '#FFFFFF', background: !ownBorder ? RING : 'transparent' }}>
              Match fill <span style={{ opacity: 0.6 }}>(a darker shade)</span>
            </button>
            <div role="radiogroup" aria-label="Stroke weight" className="-mx-1 flex gap-1 border-t pt-2" style={{ borderColor: 'rgba(255,255,255,.12)' }}>
              {WEIGHTS.map(w => (
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
      {card && onOpen && (
        <>
          <button type="button" data-testid="session-card-open" onClick={onOpen}
            className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-[13px] font-medium hover:bg-white/10" style={{ color: '#FFFFFF' }}>
            <ArrowUpRight size={15} aria-hidden /> Open
          </button>
          {rule}
        </>
      )}
      {shapes.length === 1 && shapes[0].kind === 'section' && (
        <>
          <GoalPopover goal={shapes[0].goal} onGoal={onGoal} />
          <button type="button" data-testid="section-access-button" aria-pressed={access === 'deny'}
            aria-label={access === 'deny' ? 'Protected from the conductor' : 'Open to the conductor'}
            title={access === 'deny' ? 'The conductor cannot change this section or anything in it. Click to open it.' : 'The conductor can change this section. Click to protect it and everything in it.'}
            onClick={onAccess} className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium hover:bg-white/10"
            style={{ color: '#FFFFFF', background: access === 'deny' ? 'rgba(151,71,255,.45)' : undefined }}>
            {access === 'deny' ? <ShieldCheck size={14} /> : <Shield size={14} />} {access === 'deny' ? 'Protected' : 'Open'}
          </button>
          {rule}
        </>
      )}
      <button type="button" data-testid="board-ask-button" onClick={onAsk} title="Ask conductor (C)" aria-keyshortcuts="C"
        className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium hover:bg-white/10" style={{ color: '#FFFFFF' }}>
        <Sparkles size={14} /> Ask conductor <kbd className="ml-0.5 rounded border px-1 text-[10px] font-medium" style={{ borderColor: 'rgba(255,255,255,.3)', opacity: 0.75 }}>C</kbd>
      </button>
      <button type="button" aria-label="Delete" title="Delete" onClick={onDelete} className="rounded-lg p-1.5 hover:bg-white/10" style={{ color: 'rgba(255,255,255,.8)' }}>
        <Trash2 size={14} />
      </button>
    </div>
  )
}
