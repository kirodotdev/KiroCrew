import { useEffect, useState } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { ROW_TITLE_CLS } from '../../components/listShell'
import type { PeekTitle } from './scrollPeek'

/** Horizontal padding of a peeked title; the chip starts this far left of the
 *  row's own title so the text lands exactly on top of it. */
const PAD_X = 4

/** How long a chip takes to fade in, and the whole layer to fade out. The
 *  `duration-[120ms]` classes below carry the same number. */
export const PEEK_FADE_MS = 120

const NO_TITLES: readonly PeekTitle[] = []

const prefersReducedMotion = () =>
  typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches

/**
 * The scroll peek's floating layer: each clipped title in view, drawn in full
 * over its row and past the sidebar's edge.
 *
 * Portaled to the body-level overlay root and `position: fixed`, so neither the sidebar's
 * width nor its overflow is involved. `pointer-events: none` on the whole
 * layer: it never takes a click, so a click on a peeked title selects the row
 * under it and a click past the sidebar reaches the chat. It is decorative
 * duplication of text the row already carries (in its `title` attribute too),
 * so it is hidden from assistive technology.
 *
 * Each chip is a plain elevated background in the row's own type, with no
 * border, shadow or heavier weight, so it reads as the row's title continued
 * rather than as a new surface.
 *
 * Chips fade in as they mount. When the peek ends the last chips stay mounted
 * at opacity 0 for one fade, then the layer goes; a peek that starts again
 * inside that window takes the layer back at once. Under reduced motion both
 * are instant (the global rule zeroes the entry animation, and the layer is
 * dropped on the render the peek ends). The layer stays click-through while it
 * fades, so a press that ends the peek is never blocked by it.
 */
export default function ScrollPeekLayer({ titles }: { titles: readonly PeekTitle[] }) {
  const live = titles.length > 0
  // The last titles the peek showed, kept so they can fade out.
  const [held, setHeld] = useState<readonly PeekTitle[]>(titles)
  if (live && held !== titles) setHeld(titles)
  const fading = !live && held.length > 0
  const reduce = fading && prefersReducedMotion()

  useEffect(() => {
    // Under reduced motion nothing is drawn while `fading`, so nothing waits.
    if (!fading || reduce) return
    const id = window.setTimeout(() => setHeld(NO_TITLES), PEEK_FADE_MS)
    return () => window.clearTimeout(id)
  }, [fading, reduce])

  const shown = live ? titles : reduce ? NO_TITLES : held
  if (shown.length === 0) return null
  const chips = shown.map(t => (
    <div
      key={t.key}
      data-scroll-peek-title=""
      className={`absolute ${ROW_TITLE_CLS} text-text bg-bg-elevated rounded-md whitespace-nowrap overflow-hidden text-ellipsis animate-in fade-in-0 duration-[120ms] ease-out`}
      style={{
        left: t.left - PAD_X,
        top: t.top,
        height: t.height,
        width: Math.min(t.width + PAD_X * 2, t.maxWidth),
        paddingLeft: PAD_X,
        paddingRight: PAD_X,
      }}
    >
      {t.text}
    </div>
  ))
  return <OverlayPortal>
    <div
      aria-hidden
      data-testid="scroll-peek-layer"
      data-fading={fading ? '' : undefined}
      className={`fixed inset-0 z-[65] pointer-events-none transition-opacity duration-[120ms] ease-out motion-reduce:transition-none ${fading ? 'opacity-0' : 'opacity-100'}`}
    >
      {chips}
    </div>
  </OverlayPortal>
}
