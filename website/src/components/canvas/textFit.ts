/**
 * A text box's height follows its text: it grows as lines wrap or are added and
 * shrinks as they go. Only its width is the person's to set.
 *
 * The file still stores a height for every text box, because JSON Canvas requires
 * one on every node, and an agent writing a canvas can only guess it. So the
 * stored height is a hint: the editor measures the real height, shows that, and
 * writes it back only when the box itself is next edited. Fitting a box the
 * person has not touched is not an edit, so loading a canvas never autosaves or
 * adds an undo step.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from 'react'
import { defaultFontSize, type Canvas, type Shape } from './canvasModel'

const LINE_HEIGHT = 1.3
const PAD_Y = 4
const PAD_X = 2
// A zero-width space: it holds a line open in the measure and is never shown.
const HOLD_LINE = String.fromCharCode(0x200b)

/** How a text box's text is set. The box draws it and the measure reads it, so both must agree. */
export function textBoxType(s: Pick<Shape, 'kind' | 'fontSize' | 'bold'>): CSSProperties {
  return {
    fontSize: s.fontSize ?? defaultFontSize(s.kind), lineHeight: LINE_HEIGHT, padding: `${PAD_Y}px ${PAD_X}px`,
    fontWeight: s.bold ? 700 : 500,
  }
}

/**
 * A height from the text's own lines, for writing an edited box nothing can lay
 * out. Never shown: it ignores wrapping, so it is shorter than a stored height
 * that allowed for wrapped lines.
 */
function estimate(s: Shape): number {
  const px = s.fontSize ?? defaultFontSize(s.kind)
  const lines = Math.max(1, s.text.split('\n').length)
  return Math.ceil(lines * px * LINE_HEIGHT + PAD_Y * 2)
}

/**
 * The height *s*'s text takes at its width, measured in *probe*: a hidden element
 * inside the canvas, so it inherits the canvas's font. Null when nothing lays the
 * probe out (no probe yet, or the canvas sits under a `display: none` ancestor),
 * so the box keeps its stored height until a real measure is possible.
 */
function measure(probe: HTMLElement | null, s: Shape): number | null {
  if (!probe) return null
  const type = textBoxType(s)
  Object.assign(probe.style, {
    fontSize: `${type.fontSize}px`, lineHeight: String(type.lineHeight), padding: type.padding, fontWeight: String(type.fontWeight), width: `${s.w}px`,
  })
  // A trailing newline ends a line the typist is about to fill, and an empty box
  // still holds one line: a zero-width space keeps that line in the measure.
  probe.textContent = !s.text || s.text.endsWith('\n') ? s.text + HOLD_LINE : s.text
  const h = probe.scrollHeight
  // Emptied again, so the page's text (and a search of it) never finds the copy.
  probe.textContent = ''
  return h > 0 ? Math.ceil(h) : null
}

/** Whether *a* and *b* say the same thing in every field but the height. */
function sameButHeight(a: Shape, b: Shape): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)] as (keyof Shape)[])
  for (const k of keys) if (k !== 'h' && a[k] !== b[k]) return false
  return true
}

const keyOf = (s: Shape) => `${s.w}|${s.fontSize ?? ''}|${s.bold ? 1 : 0}|${s.text}`

/**
 * The canvas as the editor shows it, every text box at its measured height, and
 * `toFile`, which turns a canvas the editor built from that view back into what
 * the file should say: a box the edit left alone keeps its stored height, and a
 * box the edit touched is written at its fitted height.
 */
export function useFittedCanvas(stored: Canvas, host: RefObject<HTMLElement | null>): { canvas: Canvas; toFile: (c: Canvas) => Canvas } {
  const [probe, setProbe] = useState<HTMLElement | null>(null)
  // Bumped when web fonts finish loading, since text set in a fallback font measures differently.
  const [fonts, setFonts] = useState(0)
  // Bumped when the canvas's laid-out width changes, including when a canvas
  // mounted hidden (a background tab) is first shown: only then can it measure.
  const [layout, setLayout] = useState(0)
  const heights = useRef(new Map<string, number>())
  /** Each stored text box's fitted copy, and the stored box behind each copy. */
  const fitted = useRef(new WeakMap<Shape, Shape>())
  const storedOf = useRef(new WeakMap<Shape, Shape>())
  /** Every shape the file has held: one of these reaching `toFile` is not an edit. */
  const known = useRef(new WeakSet<Shape>())
  /** The file's current shapes by id, for telling a copied but unchanged box from an edited one. */
  const storedById = useRef(new Map<string, Shape>())
  const probeRef = useRef(probe)
  probeRef.current = probe

  useLayoutEffect(() => {
    const el = host.current
    if (!el) return
    const p = document.createElement('div')
    p.setAttribute('aria-hidden', 'true')
    Object.assign(p.style, {
      position: 'absolute', left: '-100000px', top: '0', visibility: 'hidden', pointerEvents: 'none',
      boxSizing: 'border-box', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
    })
    el.appendChild(p)
    heights.current.clear()
    setProbe(p)
    return () => { p.remove(); setProbe(null) }
  }, [host])

  useEffect(() => {
    const el = host.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let width = el.clientWidth
    const ro = new ResizeObserver(() => {
      const w = el.clientWidth
      if (w === width) return
      width = w
      // Going hidden keeps what was measured; there is nothing new to read.
      if (w === 0) return
      heights.current.clear()
      setLayout(n => n + 1)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [host])

  useEffect(() => {
    const set = typeof document !== 'undefined' ? document.fonts : undefined
    if (!set?.addEventListener) return
    let live = true
    const bump = () => { if (live) { heights.current.clear(); setFonts(n => n + 1) } }
    set.addEventListener('loadingdone', bump)
    void set.ready?.then(bump)
    return () => { live = false; set.removeEventListener('loadingdone', bump) }
  }, [])

  /** *s*'s fitted height, or null while it cannot be measured. Only real measures are kept. */
  const heightOf = useCallback((s: Shape): number | null => {
    const key = keyOf(s)
    const kept = heights.current.get(key)
    if (kept !== undefined) return kept
    const h = measure(probeRef.current, s)
    if (h !== null) heights.current.set(key, h)
    return h
  }, [])

  const canvas = useMemo(() => {
    // Read so a new probe, a font load or a new layout measures again.
    void probe
    void fonts
    void layout
    let changed = false
    storedById.current = new Map(stored.shapes.map(s => [s.id, s]))
    const shapes = stored.shapes.map(s => {
      known.current.add(s)
      if (s.kind !== 'text') return s
      const h = heightOf(s)
      if (h === null || h === s.h) return s
      let f = fitted.current.get(s)
      if (!f || f.h !== h) {
        f = { ...s, h }
        fitted.current.set(s, f)
        storedOf.current.set(f, s)
      }
      changed = true
      return f
    })
    return changed ? { ...stored, shapes } : stored
  }, [stored, probe, fonts, layout, heightOf])

  const toFile = useCallback((c: Canvas): Canvas => {
    let changed = false
    const shapes = c.shapes.map(s => {
      const was = storedOf.current.get(s)
      let out = s
      if (was) out = was
      else if (s.kind === 'text' && !known.current.has(s)) {
        // An edit can copy a box it does not change (unlocking every shape does):
        // only a box that differs from the file in more than its height is edited.
        const prev = storedById.current.get(s.id)
        if (prev && sameButHeight(prev, s)) out = prev
        else {
          // An edited box is written at a height that holds its text, even where
          // nothing lays it out: a larger font must not keep the old height.
          const h = heightOf(s) ?? estimate(s)
          if (h !== s.h) out = { ...s, h }
        }
      }
      if (out !== s) changed = true
      return out
    })
    return changed ? { ...c, shapes } : c
  }, [heightOf])

  return { canvas, toFile }
}
