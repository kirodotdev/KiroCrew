import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer, { reservedImageStyle, reservedImageClass } from '../components/MarkdownRenderer'

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

describe('learned image dimensions (exact reserve on remount)', () => {
  // The 120px min-height floor bounds the first-ever load, but a transcript
  // image REMOUNTS whenever the virtualized window scrolls back over it — a
  // 400-600px screenshot re-realizes (natural - 120)px of shift on every
  // remount. Recording naturalWidth/Height on load (imageDims, keyed by
  // resolved URL) lets the next mount reserve the exact aspect box via
  // width/height attributes.
  it('records natural dimensions on load and reserves them exactly on the next mount', () => {
    localStorage.clear()
    const md = '![shot](https://example.com/learned-shot.png)'
    const first = render(<MarkdownRenderer content={md} />)
    const img1 = first.container.querySelector('img') as HTMLImageElement
    expect(img1).not.toBeNull()
    // First mount: no learned dims yet -> heuristic floor, no width/height attrs.
    expect(img1.hasAttribute('width')).toBe(false)
    Object.defineProperty(img1, 'naturalWidth', { configurable: true, get: () => 780 })
    Object.defineProperty(img1, 'naturalHeight', { configurable: true, get: () => 1688 })
    fireEvent.load(img1)
    first.unmount()
    // Second mount (virtualized remount): exact reserve BEFORE any bytes.
    const second = render(<MarkdownRenderer content={md} />)
    const img2 = second.container.querySelector('img') as HTMLImageElement
    // The reserve must replicate the replaced-element min/max resolution,
    // including the max-height cap BACK-PROPAGATING into the width (a tall
    // screenshot capped at 60vh also narrows). width/height attributes and a
    // bare aspect-ratio both fail that transfer — the box goes full-width
    // with the image letterboxed centered inside and the border wrapping the
    // empty band (the reported regression). So the width must carry the
    // min(natural, cap × ratio) expression and no width attribute may remain.
    expect(img2.hasAttribute('width')).toBe(false)
    // The reserve arithmetic lives in the `.mc-img-reserve` stylesheet rule
    // (min()/calc() back-propagating the height cap into the width); the
    // component contributes only the class and the two numeric properties.
    expect(img2.className).toContain('mc-img-reserve')
    expect(img2.getAttribute('style') ?? '').toContain('--mc-img-w: 780')
    expect(img2.getAttribute('style') ?? '').toContain('--mc-img-h: 1688')
    // The heuristic floor must yield to the exact reserve (no stacked minHeight).
    expect(img2.style.minHeight === '' || img2.style.minHeight === undefined || img2.style.minHeight !== '120px').toBe(true)
    second.unmount()
  })

  it('a failed load teaches nothing (zero natural size is not recorded)', () => {
    localStorage.clear()
    const md = '![broken](https://example.com/broken-shot.png)'
    const first = render(<MarkdownRenderer content={md} />)
    const img1 = first.container.querySelector('img') as HTMLImageElement
    Object.defineProperty(img1, 'naturalWidth', { configurable: true, get: () => 0 })
    Object.defineProperty(img1, 'naturalHeight', { configurable: true, get: () => 0 })
    fireEvent.load(img1)
    first.unmount()
    const second = render(<MarkdownRenderer content={md} />)
    const img2 = second.container.querySelector('img') as HTMLImageElement
    // Nothing learned -> heuristic floor, no exact-reserve style.
    expect(img2.style.aspectRatio === '' || img2.style.aspectRatio === undefined).toBe(true)
    expect(img2.style.minHeight).toBe('120px')
    second.unmount()
  })
})

describe('reservedImageStyle / reservedImageClass: the reserve contract', () => {
  it('passes the intrinsic size as NUMBERS, leaving the arithmetic to the stylesheet', () => {
    const st = reservedImageStyle({ w: 780, h: 1688 }) as Record<string, unknown>
    expect(st['--mc-img-w']).toBe(780)
    expect(st['--mc-img-h']).toBe(1688)
    // No CSS-shaped strings: a value string here would both bypass the
    // stylesheet and read as untranslated copy to the i18n gate.
    for (const v of Object.values(st)) expect(typeof v).toBe('number')
  })
  it('selects the compact height cap via the mode class', () => {
    expect(reservedImageClass(true).split(' ')).toContain('mc-img-reserve-compact')
    expect(reservedImageClass(false).split(' ')).not.toContain('mc-img-reserve-compact')
    expect(reservedImageClass(false)).toContain('mc-img-reserve')
  })
})


describe('sent-prompt image alignment (coupled with the bubble shrink-wrap)', () => {
  it('aligns a compact image to the end edge with a DEFINITE width cap', () => {
    const md = '![a](https://x.test/align.png)'
    const compact = render(<MarkdownRenderer content={md} compactImages />)
    const img = compact.container.querySelector('img')!
    // ms-auto sits on the IMG, never on the wrapper: a shrink-to-fit wrapper
    // makes a percentage max-width resolve against its own content, silently
    // dropping the cap and scattering mixed-width images.
    expect(img.className).toContain('ms-auto')
    expect(img.parentElement!.className).not.toContain('w-fit')
    // The cap must be DEFINITE. A percentage (min(100%,240px)) makes the
    // image's max-content contribution indefinite, so UserMessage's `w-fit`
    // bubble falls back to the full available width and the empty band that
    // end-alignment creates never closes.
    expect(img.className).toContain('max-w-[240px]')
    expect(img.className).not.toContain('min(100%,240px)')
    compact.unmount()
  })

  it('leaves response images at the start edge with their own cap', () => {
    const normal = render(<MarkdownRenderer content={'![a](https://x.test/a.png)'} />)
    const img = normal.container.querySelector('img')!
    expect(img.className).not.toContain('ms-auto')
    expect(img.className).toContain('max-w-[min(100%,760px)]')
    normal.unmount()
  })
})
