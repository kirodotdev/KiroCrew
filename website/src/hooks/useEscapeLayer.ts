import { useEffect, useRef } from 'react'
import { useLocalCrewHidden } from '../components/overlayRoot'

/* One owner for page-wide Escape (issue #25202).
 *
 * Several surfaces answer Escape from a page-wide listener. Each used to pick
 * its own phase and target, so which one got a press depended on capture vs
 * bubble and registration order, and one press could do two things. A surface
 * that answers a page-wide Escape registers a LAYER here instead. ONE document
 * capture listener asks the active layers in order, highest priority first and
 * the most recently opened first within a priority, and each answers:
 *
 *   - 'take'  -- it acted; the press is consumed (preventDefault +
 *                stopPropagation), so nothing else sees it;
 *   - 'pass'  -- not mine; ask the next layer;
 *   - 'yield' -- leave the press untouched for an owner outside the stack (an
 *                open picker, IME composition, dictation, a dialog,
 *                `[data-escape-owner]`), and ask no other layer.
 *
 * A layer stands down while its local Crew is hidden behind a remote one.
 * Listeners that act only on a press nobody claimed (the notification banner)
 * stay on window bubble and skip a `defaultPrevented` Escape.
 *
 * Document, not window: a take's stopPropagation then still lets every other
 * document-capture listener run (stopping speech read-back, which marks the
 * press so the composer gate yields; list menus), as the flyout's own capture
 * listener did, while window-capture dialogs still answer first. */

export type EscapeVerdict = 'take' | 'pass' | 'yield'

/** Layer priorities; higher answers first. */
export const ESCAPE_PRIORITY = { composer: 0, flyout: 10 } as const

interface Layer { seq: number; priority: number; handle: (e: KeyboardEvent) => EscapeVerdict }

const layers: Layer[] = []
let nextSeq = 0

function onKeyDown(e: KeyboardEvent): void {
  if (e.key !== 'Escape' || e.defaultPrevented) return
  const ordered = [...layers].sort((a, b) => b.priority - a.priority || b.seq - a.seq)
  for (const layer of ordered) {
    const verdict = layer.handle(e)
    if (verdict === 'pass') continue
    if (verdict === 'take') { e.preventDefault(); e.stopPropagation() }
    return
  }
}

function push(layer: Layer): () => void {
  if (layers.length === 0) document.addEventListener('keydown', onKeyDown, true)
  layers.push(layer)
  return () => {
    const i = layers.indexOf(layer)
    if (i >= 0) layers.splice(i, 1)
    if (layers.length === 0) document.removeEventListener('keydown', onKeyDown, true)
  }
}

/** Answer page-wide Escape while `active`. `onEscape` is read through a ref, so
 *  a new callback identity never re-registers (and never reorders) the layer;
 *  returning nothing means 'take'. */
export function useEscapeLayer(active: boolean, onEscape: (e: KeyboardEvent) => EscapeVerdict | void, priority = 0): void {
  const hidden = useLocalCrewHidden()
  const handler = useRef(onEscape)
  handler.current = onEscape
  useEffect(() => {
    if (!active || hidden) return
    return push({ seq: ++nextSeq, priority, handle: e => handler.current(e) ?? 'take' })
  }, [active, hidden, priority])
}
