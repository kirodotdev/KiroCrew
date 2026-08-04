import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * REGRESSION GUARD — gap #1: IMAGES.
 *
 * The streaming row's height is smoothed so it does not debounce into a spacer
 * "flash" for a scrolled-up user, but that smoothing does not address content
 * that changes height on a one-shot async event rather than gradual text
 * growth. A markdown image (`ImgWithFallback` in MarkdownRenderer) renders:
 *
 *     <img src=… loading="lazy"
 *          className="max-w-[min(100%,760px)] max-h-[60vh] object-contain …" />
 *
 * with NO reserved intrinsic dimensions (only non-SVG). Before the bytes decode
 * the element has ~0 layout height; on load it snaps to its natural height. That
 * snap shifts every sibling below the image inside the same bubble (classic CLS)
 * — visible as a flash/jump while the message is streaming. Reserving space for
 * the image (explicit width/height, an aspect-ratio box, or a min-height
 * placeholder) is what removes the shift; the virtualizer spacer smoothing
 * cannot, because the whole image height arrives in a single RO tick.
 *
 * These tests assert that the image reserves vertical space before it loads.
 *
 * jsdom has no CSS/layout engine, so we assert on the MECHANISM
 * (dimension attributes / space-reserving inline style) rather than on measured
 * pixels — the only fix-agnostic signal available pre-load.
 */

const STREAM = { streaming: true, glow: true, smooth: true } as const

/** True when the <img> (or its wrapper) reserves vertical layout space before
 *  the bytes load — via width+height attributes, an aspect-ratio, an explicit
 *  height, or a min-height placeholder. Fix-agnostic across those approaches. */
function reservesVerticalSpace(img: HTMLImageElement): boolean {
  const hasWH = img.hasAttribute('width') && img.hasAttribute('height')
  const spaceStyle = (st: CSSStyleDeclaration | undefined): boolean => {
    if (!st) return false
    const ar = st.aspectRatio
    const mh = st.minHeight
    const h = st.height
    return (
      (!!ar && ar !== 'auto' && ar !== '') ||
      (!!mh && mh !== '0px' && mh !== '') ||
      (!!h && h !== 'auto' && h !== '')
    )
  }
  return hasWH || spaceStyle(img.style) || spaceStyle(img.parentElement?.style)
}

describe('streaming image layout-shift regression (gap #1)', () => {
  it('PREMISE: a markdown image renders an <img> element', () => {
    // A quick check so a rendering regression is distinguishable
    // from the reserved-space assertion below.
    const { container } = render(
      <MarkdownRenderer content={'![diagram](https://example.com/diagram.png)'} {...STREAM} />,
    )
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('GAP: a raster markdown image reserves vertical space before it loads (no on-load shift)', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'Here is the chart:\n\n![chart](https://example.com/chart.png)\n\nand text below it.'}
        {...STREAM}
      />,
    )
    const img = container.querySelector('img') as HTMLImageElement | null
    expect(img).not.toBeNull()
    // Regression guard: the <img> must reserve vertical space before load
    // (min-height placeholder here) so on-load it does not pop from 0 to its
    // natural height and shove the "and text below it." paragraph down.
    expect(reservesVerticalSpace(img!)).toBe(true)
  })
})
