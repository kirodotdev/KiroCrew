import { lookPreviewSrc } from '../utils/lookPreview'

/**
 * The real dashboard, scaled down: the first-run "Pick your look" step's live
 * preview of what the mode, the color theme and the Translucent panels switch
 * change. A same-origin iframe of `/chat?look-preview=1` (see
 * `utils/lookPreview.ts` for how the frame mirrors the parent's picks) renders
 * at a narrow desktop width and is transformed to the box, so the box is a
 * 16:9 picture of the product at whatever width the step gives it. The
 * frame's storage, network and channel writes are fenced by
 * `utils/lookPreviewBoot.ts`.
 *
 * Illustrative only: `aria-hidden`, `inert`, no pointer events, not in the tab
 * order -- the user picks on the controls under it, never on the picture.
 */
// 16:9 at a narrow desktop width: the smaller the frame, the larger the
// composer, the chips and the text read once scaled into the step's box, and
// what the Translucent panels switch changes is on those.
const FRAME_WIDTH = 800
const FRAME_HEIGHT = 450

export function LookPreview({ className = '' }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      data-testid="look-preview"
      className={`relative overflow-hidden rounded-xl border border-border bg-bg shadow-lg pointer-events-none select-none ${className}`}
      style={{ aspectRatio: `${FRAME_WIDTH} / ${FRAME_HEIGHT}`, containerType: 'inline-size' }}
    >
      {/* eslint-disable-next-line jsx-a11y/iframe-has-title -- the frame is a
          picture inside an aria-hidden, inert box: nothing announces it. */}
      <iframe
        tabIndex={-1}
        // @ts-expect-error -- `inert` is not in React's iframe attribute types yet; the browser honours it.
        inert=""
        src={lookPreviewSrc()}
        style={{
          width: FRAME_WIDTH,
          height: FRAME_HEIGHT,
          border: 0,
          transformOrigin: '0 0',
          transform: `scale(calc(100cqw / ${FRAME_WIDTH}px))`,
          pointerEvents: 'none',
        }}
      />
    </div>
  )
}
