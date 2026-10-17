import { createPortal } from 'react-dom'
import { ROW_TITLE_CLS } from '../../components/listShell'
import type { PeekTitle } from './scrollPeek'

/** Horizontal padding of a peeked title; the chip starts this far left of the
 *  row's own title so the text lands exactly on top of it. */
const PAD_X = 4

/**
 * The scroll peek's floating layer: each clipped title in view, drawn in full
 * over its row and past the sidebar's edge.
 *
 * Portaled to `document.body` and `position: fixed`, so neither the sidebar's
 * width nor its overflow is involved. `pointer-events: none` on the whole
 * layer: it never takes a click, so a click on a peeked title selects the row
 * under it and a click past the sidebar reaches the chat. It is decorative
 * duplication of text the row already carries (in its `title` attribute too),
 * so it is hidden from assistive technology.
 */
export default function ScrollPeekLayer({ titles }: { titles: readonly PeekTitle[] }) {
  if (titles.length === 0) return null
  return createPortal(
    <div aria-hidden="true" data-testid="scroll-peek-layer" className="fixed inset-0 z-[65] pointer-events-none">
      {titles.map(t => (
        <div
          key={t.key}
          data-scroll-peek-title=""
          className={`absolute ${ROW_TITLE_CLS} font-semibold text-text bg-bg-elevated border border-border rounded-md shadow-md whitespace-nowrap overflow-hidden text-ellipsis`}
          style={{
            left: t.left - PAD_X - 1,
            top: t.top - 1,
            height: t.height + 2,
            width: Math.min(t.width + PAD_X * 2 + 2, t.maxWidth),
            paddingLeft: PAD_X,
            paddingRight: PAD_X,
          }}
        >
          {t.text}
        </div>
      ))}
    </div>,
    document.body,
  )
}
