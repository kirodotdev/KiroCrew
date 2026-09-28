/**
 * BoardFrame — renders a style or art-direction HTML document scaled to fit.
 *
 * These documents are agent-authored HTML (or user-imported) at a fixed 1920x1080
 * slide size. They are rendered in a **sandboxed iframe with no `allow-scripts`
 * and no `allow-same-origin`**, so the document gets a null origin and cannot
 * execute script or reach the dashboard. The iframe is `pointer-events: none` and
 * laid out at intrinsic size, then scaled by transform so the board fills the
 * panel width at any container size.
 *
 * `srcDoc` rather than the artifact URL: it keeps the document in the sandboxed
 * null origin instead of loading it as a navigation on the dashboard origin.
 *
 * **Both frames here MUST go through `prepareBoardHtml`,** which prepends an
 * egress-denying CSP. The sandbox stops script, but a `srcDoc` document is not
 * covered by the server's response headers at all, and an empty sandbox does
 * nothing about PASSIVE loads — one `<img src="https://…">` in an agent-written
 * board is a GET carrying deck content off-origin with no script involved. The
 * scaled preview and the thumbnail are the same document at two sizes, so a policy
 * on only one of them is not a policy.
 */

import { useCallback, useRef, useState } from 'react'
import { i18nT } from '../../i18n/t'
import { BOARD_GAP, BOARD_HEIGHT, BOARD_WIDTH, countBoardSlides, prepareBoardHtml } from './lib'

interface BoardFrameProps {
  html: string
  title: string
}

export default function BoardFrame({ html, title }: BoardFrameProps) {
  const [width, setWidth] = useState(0)
  const observerRef = useRef<ResizeObserver | null>(null)

  // Ref callback + ResizeObserver rather than a layout effect: the panel is
  // resizable, so the scale has to track the container continuously.
  const measure = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!node) return
    const initial = node.getBoundingClientRect().width
    if (initial > 0) setWidth(initial)
    if (typeof ResizeObserver === 'undefined') return
    observerRef.current = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0
      if (next > 0) setWidth(next)
    })
    observerRef.current.observe(node)
  }, [])

  const slides = countBoardSlides(html)
  const scale = width > 0 ? width / BOARD_WIDTH : 0
  const intrinsicHeight = (BOARD_HEIGHT + BOARD_GAP) * slides

  return (
    <div ref={measure} className="w-full">
      {scale > 0 ? (
        <div style={{ width: '100%', height: intrinsicHeight * scale, overflow: 'hidden' }}>
          <iframe
            srcDoc={prepareBoardHtml(html)}
            sandbox=""
            title={title}
            tabIndex={-1}
            className="pointer-events-none border-none"
            style={{
              width: `${BOARD_WIDTH}px`,
              height: `${intrinsicHeight}px`,
              // `translateZ(0)` composed onto the scale, not instead of it: the
              // scale is the board's whole geometry. A 2D transform makes a
              // stacking context but does NOT force this frame onto its own
              // compositing layer, so an engine can skip its first paint and
              // leave the preview blank. Same remedy as ArtifactThumbs.
              transform: `scale(${scale}) translateZ(0)`,
              transformOrigin: 'top left',
            }}
          />
        </div>
      ) : (
        <div className="text-sm text-muted">{i18nT('apps.pptxMaker.boardFrame.loading')}</div>
      )}
    </div>
  )
}

/**
 * BoardThumb — a tiny non-interactive preview of a style's cover slide.
 *
 * Same isolation as BoardFrame (empty `sandbox`, no scripts, no same-origin),
 * scaled to a fixed pixel width for the library list.
 */
export function BoardThumb({
  html,
  width = 52,
  title,
}: {
  html: string
  width?: number
  /** Iframe title. Decorative by default (the row's own text names the style),
   *  but overridable so a caller can label it when it stands alone. */
  title?: string
}) {
  const dimensions = { width: `${width}px`, height: `${(width * 9) / 16}px` }
  if (!html) {
    return <div className="rounded bg-bg-elevated shrink-0 border border-border" style={dimensions} />
  }
  return (
    <div
      className="rounded overflow-hidden shrink-0 border border-border bg-bg-elevated"
      style={dimensions}
    >
      <iframe
        srcDoc={prepareBoardHtml(html)}
        sandbox=""
        tabIndex={-1}
        title={title ?? i18nT('apps.pptxMaker.boardFrame.thumbnail')}
        aria-hidden="true"
        className="pointer-events-none border-none"
        style={{
          width: `${BOARD_WIDTH}px`,
          height: `${BOARD_HEIGHT}px`,
          // Composed onto the scale for the same reason as BoardFrame above:
          // promotion without losing the thumbnail's geometry.
          transform: `scale(${width / BOARD_WIDTH}) translateZ(0)`,
          transformOrigin: 'top left',
        }}
      />
    </div>
  )
}
