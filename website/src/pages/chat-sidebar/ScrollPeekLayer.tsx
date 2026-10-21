import { OverlayPortal } from '../../components/overlayRoot'
import { ROW_TITLE_CLS } from '../../components/listShell'
import type { PeekTitle } from './scrollPeek'

/** Horizontal padding of a peeked title; the chip starts this far left of the
 *  row's own title so the text lands exactly on top of it. */
const PAD_X = 4

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
 */
export default function ScrollPeekLayer({ titles }: { titles: readonly PeekTitle[] }) {
  const chips = titles.map(t => (
    <div
      key={t.key}
      data-scroll-peek-title=""
      className={`absolute ${ROW_TITLE_CLS} text-text bg-bg-elevated rounded-md whitespace-nowrap overflow-hidden text-ellipsis`}
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
  if (titles.length === 0) return null
  return <OverlayPortal>
    <div aria-hidden data-testid="scroll-peek-layer" className="fixed inset-0 z-[65] pointer-events-none">
      {chips}
    </div>
  </OverlayPortal>
}
