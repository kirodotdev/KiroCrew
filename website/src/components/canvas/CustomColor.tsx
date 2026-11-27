/**
 * A custom colour picker, after FigJam's: a hex field with an eyedropper, a hue
 * bar and a saturation and brightness square, in the swatch panel it sits in.
 *
 * It reports every change as it happens (`final` false while a handle is being
 * dragged or a hex typed) and once more when the gesture ends (`final` true), so
 * the canvas can show the colour live and record it as one undo step. `fresh`
 * marks the first value of each gesture, even if the previous one never ended.
 */

import { Pipette } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useImeGuard } from '../../hooks/useImeGuard'

import { hexToHsv, hsvToHex, toHex, type Hsv } from './colorMath'

interface Props {
  /** The colour now, or undefined when the shape uses a swatch. */
  hex: string | undefined
  /** Where the picker starts when there is no custom colour yet. */
  start: string
  onHex: (hex: string, final: boolean, fresh: boolean) => void
}


interface EyeDropperApi { open: () => Promise<{ sRGBHex: string }> }
const eyeDropper = (): EyeDropperApi | null => {
  const Ctor = (window as unknown as { EyeDropper?: new () => EyeDropperApi }).EyeDropper
  return Ctor ? new Ctor() : null
}

/** Reports a 0-1 position inside *el* for each pointer move while a press holds, then once more on release. */
function useDrag(onMove: (x: number, y: number, final: boolean, fresh: boolean) => void) {
  const el = useRef<HTMLDivElement>(null)
  const at = (e: { clientX: number; clientY: number }, final: boolean, fresh = false) => {
    const r = el.current?.getBoundingClientRect()
    if (!r || !r.width || !r.height) return
    onMove(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)), final, fresh)
  }
  return {
    ref: el,
    onPointerDown: (e: React.PointerEvent) => {
      e.preventDefault()
      e.currentTarget.setPointerCapture?.(e.pointerId)
      at(e, false, true)
    },
    onPointerMove: (e: React.PointerEvent) => { if (e.buttons) at(e, false) },
    onPointerUp: (e: React.PointerEvent) => at(e, true),
  }
}

export default function CustomColor({ hex, start, onHex }: Props) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [hsv, setHsv] = useState<Hsv>(() => hexToHsv(hex ?? start))
  const [text, setText] = useState(hex ?? start)
  const typing = useRef(false)
  /** Typing has changed the colour on the canvas in an unfinished gesture. */
  const dirty = useRef(false)

  // Follow the colour from outside (another shape picked, an undo), but not while typing.
  // A hex that is the picker's own colour coming back keeps the picker's HSV: black and
  // greys carry no hue (and black no saturation), so re-deriving them would move the handles.
  useEffect(() => {
    if (hex && !typing.current) {
      setHsv(prev => (hsvToHex(prev) === hex ? prev : hexToHsv(hex)))
      setText(hex)
    }
  }, [hex])

  const apply = (next: Hsv, final: boolean, fresh: boolean) => {
    setHsv(next)
    const h = hsvToHex(next)
    setText(h)
    onHex(h, final, fresh)
  }

  const square = useDrag((x, y, final, fresh) => apply({ h: hsv.h, s: x, v: 1 - y }, final, fresh))
  const hue = useDrag((x, _y, final, fresh) => apply({ ...hsv, h: Math.min(359.99, x * 360) }, final, fresh))
  const current = hsvToHex(hsv)
  const dropper = eyeDropper()

  return (
    <div data-testid="custom-color" className="flex w-[240px] flex-col gap-3 pt-1">
      <div className="flex items-center gap-2">
        {dropper && (
          <button type="button" aria-label={t('components.canvas.eyedropper_label')} title={t('components.canvas.eyedropper')}
            onClick={() => { void dropper.open().then(r => { const h = toHex(r.sRGBHex); if (mounted.current && h) apply(hexToHsv(h), true, true) }).catch(() => undefined) }}
            className="rounded-lg p-1.5 text-text hover:bg-bg-hover">
            <Pipette size={16} />
          </button>
        )}
        <input aria-label={t('components.canvas.hex_colour')} value={text} spellCheck={false} maxLength={9}
          onChange={e => {
            setText(e.target.value)
            const h = toHex(e.target.value)
            if (h) {
              setHsv(hexToHsv(h))
              const fresh = !dirty.current
              dirty.current = true
              onHex(h, false, fresh)
            }
          }}
          // An IME's committing Enter must not leave the field, so Enter commits only once claimed.
          {...ime.bindComposition<HTMLInputElement>({
            onFocus: e => { typing.current = true; e.currentTarget.select() },
            onBlur: () => {
              typing.current = false
              const h = toHex(text) ?? current
              setText(h)
              // Finish what typing showed, as one step. Only typing finishes anything:
              // a shape coloured by a swatch has no hex to compare with, so focusing the
              // field and leaving it would otherwise recolour the shape to the shown start.
              if (dirty.current) onHex(h, true, false)
              dirty.current = false
            },
          })}
          onKeyDown={e => { e.stopPropagation(); if (e.key === 'Enter' && ime.claimEnter(e)) e.currentTarget.blur() }}
          className="w-full rounded-lg px-2.5 py-1.5 text-[13px]"
          style={{ background: 'var(--bg)', color: 'var(--text)', border: `1px solid ${text && !toHex(text) ? 'var(--danger)' : 'var(--border)'}` }} />
      </div>
      <div ref={hue.ref} data-testid="custom-hue" role="slider" aria-label={t('components.canvas.hue')} aria-valuemin={0} aria-valuemax={360} aria-valuenow={Math.round(hsv.h)}
        onPointerDown={hue.onPointerDown} onPointerMove={hue.onPointerMove} onPointerUp={hue.onPointerUp}
        className="relative h-3 cursor-pointer rounded-full"
        style={{ background: 'linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)', touchAction: 'none' }}>
        <span className="pointer-events-none absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{ left: `${(hsv.h / 360) * 100}%`, background: hsvToHex({ h: hsv.h, s: 1, v: 1 }), border: '2.5px solid #FFFFFF', boxShadow: '0 1px 3px rgba(0,0,0,.4)' }} />
      </div>
      <div ref={square.ref} data-testid="custom-square" role="slider" aria-label={t('components.canvas.saturation_brightness')} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(hsv.s * 100)} aria-valuetext={current}
        onPointerDown={square.onPointerDown} onPointerMove={square.onPointerMove} onPointerUp={square.onPointerUp}
        className="relative h-[150px] cursor-crosshair rounded-lg"
        style={{
          background: `linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, transparent), ${hsvToHex({ h: hsv.h, s: 1, v: 1 })}`,
          touchAction: 'none',
        }}>
        <span className="pointer-events-none absolute h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{ left: `${hsv.s * 100}%`, top: `${(1 - hsv.v) * 100}%`, background: current, border: '2.5px solid #FFFFFF', boxShadow: '0 1px 3px rgba(0,0,0,.4), 0 0 0 1px color-mix(in srgb, var(--accent) 20%, transparent)' }} />
      </div>
    </div>
  )
}
