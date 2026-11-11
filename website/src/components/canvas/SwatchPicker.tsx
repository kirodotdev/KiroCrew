/**
 * FigJam's colour picker: one round button showing the current colour that opens
 * a floating panel with two rows, the strong colours over their light ones. For a
 * shape the panel starts with a row of modes: Fill, Transparent, No fill for the
 * fill; Solid, Dashed, None for the border. Shared by the shape toolbar and the
 * connector toolbar.
 */

import { ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { LIGHT_SWATCHES, STRONG_SWATCHES, SWATCH_STYLE, type Swatch } from './canvasModel'
import { FLOAT_SHADOW, SWATCH_DOT_BORDER } from './canvasPaint'
import CustomColor from './CustomColor'
import { swatchName } from './swatchNames'

const RING = 'var(--accent)'

interface Props {
  /** What the swatch names read as in labels: "Colour blue", "Line colour blue". */
  label: string
  /** The current colour; several shapes of different colours pass undefined. */
  value: Swatch | undefined
  /** What the button shows when *value* is unset (a connector's default ink). */
  fallback?: string
  onPick: (color: Swatch) => void
  /** Offered as the last circle of the light row, for a colour that means "default". */
  onReset?: () => void
  /** The colour a shape shows on the button, picked by *paper* (stickies) or *fill*. */
  show?: 'paper' | 'fill' | 'stroke'
  /** The mode tabs above the colours, with the current one and what picks another. */
  modes?: PickerMode[]
  mode?: string
  onMode?: (mode: string) => void
  /** What the tabs are called, for assistive tech. */
  modesLabel?: string
  /** Draws the button's colour as a ring rather than a disc (an unfilled shape). */
  ring?: boolean
  /** The button shows this instead of the colour disc (the border picker's lines icon). */
  icon?: ReactNode
  /** With this, the last circle opens a free colour picker; *hex* is the custom colour now, if any. */
  custom?: { hex: string | undefined; start: string; onHex: (hex: string, final: boolean, fresh: boolean) => void }
  /** Extra content under the colours. */
  footer?: ReactNode
}

export interface PickerMode { mode: string; label: string; icon: ReactNode }

const dot = (bg: string, selected: boolean) => ({
  width: 22, height: 22, borderRadius: '50%', background: bg, flex: 'none' as const,
  border: SWATCH_DOT_BORDER, cursor: 'pointer', padding: 0,
  outline: selected ? `2px solid ${RING}` : 'none', outlineOffset: 2,
})

export default function SwatchPicker({ label, value, fallback, onPick, onReset, show = 'fill', modes, mode, onMode, modesLabel, ring, icon, custom, footer }: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [freeform, setFreeform] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  // The panel closes on a press outside it or on Escape.
  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('pointerdown', away, true); document.removeEventListener('keydown', esc) }
  }, [open])

  const shown = custom?.hex ?? (value ? SWATCH_STYLE[value][show] : (fallback ?? 'transparent'))
  const pick = (c: Swatch) => { onPick(c); setOpen(false) }
  const swatch = (c: Swatch) => (
    <button key={c} type="button" aria-label={t('components.canvas.swatch_label', { label, colour: swatchName(c) })} title={swatchName(c)} onClick={() => pick(c)}
      style={dot(SWATCH_STYLE[c][show], value === c)} />
  )

  return (
    <div ref={root} className="static md:relative">
      <button type="button" aria-label={label} aria-expanded={open} title={label} onClick={() => setOpen(o => !o)}
        className="flex items-center gap-0.5 rounded-lg px-1 py-0.5 hover:bg-bg-hover">
        {icon ?? (
          <span className="block h-5 w-5 rounded-full"
            style={{ background: ring ? 'transparent' : shown, border: ring ? `3px solid ${shown}` : '1px solid var(--border)' }} />
        )}
        <ChevronDown size={12} className="text-muted" />
      </button>
      {open && (
        <div data-testid="swatch-panel" className="absolute flex flex-col gap-2 rounded-2xl bg-bg-elevated p-3 text-text inset-x-0 top-full mt-2 md:inset-x-auto md:left-0 md:top-auto md:bottom-full md:mt-0 md:mb-2.5"
          style={{ zIndex: 10, boxShadow: FLOAT_SHADOW }}>
          {modes && onMode && (
            <div role="radiogroup" aria-label={modesLabel} data-testid="picker-modes" className="-mx-1 flex gap-1 border-b border-border pb-2">
              {modes.map(m => (
                <button key={m.mode} type="button" role="radio" aria-checked={mode === m.mode} aria-label={m.label}
                  onClick={() => onMode(m.mode)}
                  className={`flex items-center gap-1.5 rounded-[10px] px-2.5 py-1.5 text-[13px] ${mode === m.mode ? 'bg-accent text-accent-fg' : 'hover:bg-bg-hover'}`}>
                  {m.icon}{m.label}
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2">{STRONG_SWATCHES.map(swatch)}</div>
          <div className="flex flex-wrap gap-2">
            {LIGHT_SWATCHES.map(swatch)}
            {custom && (
              <button type="button" aria-label={t('components.canvas.swatch_custom_label', { label })} aria-expanded={freeform} title={t('components.canvas.custom_colour')} onClick={() => setFreeform(f => !f)}
                className="relative flex-none rounded-full"
                style={{ width: 22, height: 22, padding: 0, border: 0, cursor: 'pointer', background: 'conic-gradient(#f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)', outline: freeform || custom.hex ? `2px solid ${RING}` : 'none', outlineOffset: 2 }}>
                <span className="absolute rounded-full" style={{ inset: 4, background: custom.hex ?? 'var(--bg-elevated)', border: '1px solid var(--border)' }} />
              </button>
            )}
            {onReset && (
              <button type="button" aria-label={t('components.canvas.swatch_default_label', { label })} title={t('components.canvas.default_colour')} onClick={() => { onReset(); setOpen(false) }}
                style={dot(fallback ?? 'transparent', value === undefined)} />
            )}
          </div>
          {custom && freeform && <CustomColor hex={custom.hex} start={custom.start} onHex={custom.onHex} />}
          {footer}
        </div>
      )}
    </div>
  )
}
