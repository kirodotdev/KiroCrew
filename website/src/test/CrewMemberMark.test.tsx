/**
 * The Crew Member brand mark (`components/CrewMemberMark.tsx`) and its use as
 * the `/members` nav icon.
 *
 * Three things are pinned here:
 * - the mark paints its asset as a CSS mask over `currentColor`, which is what
 *   lets it follow the rail's active (accent) / idle colour states instead of
 *   being a fixed-colour <img>;
 * - the `members` built-in surface renders that mark rather than a Lucide glyph
 *   (regression guard for the icon swap);
 * - the ASSET's stroke geometry matches Lucide's, so the glyph does not sit
 *   heavier or lighter than the neighbours it shares the rail with. Nothing
 *   else in the build can catch that — a wrong stroke width renders perfectly
 *   happily, just visibly off — so it is asserted against the art itself.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ReactElement } from 'react'
import { CrewMemberMark } from '../components/CrewMemberMark'
import '../surfaces/builtins'
import { getBuiltinSurface } from '../surfaces/registry'

const ASSET = join(dirname(fileURLToPath(import.meta.url)), '..', 'assets', 'crew-member-mark.svg')

describe('CrewMemberMark', () => {
  // The mask contract must be asserted against React's OWN style serialization,
  // not the test DOM: jsdom's `cssstyle` does not implement the `mask-*`
  // longhands, so React's CSSOM assignment is silently dropped and neither
  // `el.style` nor the serialized `style` attribute carries them under jsdom.
  const maskStyle = () => {
    const html = renderToStaticMarkup(<CrewMemberMark />)
    return /style="([^"]*)"/.exec(html)?.[1] ?? ''
  }

  it('paints the mark asset as a mask over currentColor', () => {
    const { getByTestId } = render(<CrewMemberMark />)
    // currentColor is what makes the glyph inherit the nav row's text colour.
    expect(getByTestId('crew-member-mark').style.backgroundColor).toBe('currentcolor')
    const style = maskStyle()
    expect(style).toContain('data:image/svg+xml') // the mark asset is the mask source
    expect(style).toContain('mask-size:contain')
  })

  it('quotes the mask URL', () => {
    // Regression guard: the bundler inlines the SVG as a `data:image/svg+xml,…`
    // URI whose attributes are single-quoted. An UNQUOTED css `url(…)` token
    // cannot contain quotes, so the browser drops the declaration and the glyph
    // paints as a solid `currentColor` square. React serializes `"` as `&quot;`.
    expect(maskStyle()).toMatch(/mask-image:url\(&quot;/)
  })

  it('is decorative (hidden from the accessibility tree)', () => {
    const { getByTestId } = render(<CrewMemberMark />)
    expect(getByTestId('crew-member-mark').getAttribute('aria-hidden')).toBe('true')
  })

  it('sizes the box from the size prop', () => {
    const { getByTestId } = render(<CrewMemberMark size={24} />)
    const el = getByTestId('crew-member-mark')
    expect(el.style.width).toBe('24px')
    expect(el.style.height).toBe('24px')
  })
})

describe('crew-member-mark.svg line weight', () => {
  const svg = readFileSync(ASSET, 'utf8')

  // Lucide's contract: every glyph is drawn on a 24×24 viewBox and stroked at
  // width 2, so `size={16}` renders a 1.33px stroke. This asset is masked at
  // `mask-size:contain` into the same 16px box, so it lands on that identical
  // 1.33px ONLY while both numbers agree with Lucide's. A SQUARE viewBox is
  // load-bearing for that: `contain` letterboxes a non-square one (which is why
  // the ghost mark's 805×1030 art reads slightly lighter), scaling the stroke by
  // something other than 16/24.
  it('is drawn on a square 24x24 viewBox, like every Lucide glyph', () => {
    expect(svg).toMatch(/viewBox="0 0 24 24"/)
  })

  it('strokes at width 2, so size=16 matches its Lucide neighbours', () => {
    // Asserted on the ROOT specifically. `stroke-width` inherits, so this is the
    // weight of both ghosts — the part that has to sit at the same weight as the
    // Lucide glyphs above and below it on the rail.
    expect(/<svg[^>]*stroke-width="2"/.test(svg)).toBe(true)
  })

  it('draws both ghosts at the inherited weight, with no per-path override', () => {
    // The pair is ONE weight on purpose. An earlier draft of this mark (a ghost
    // inside a speech bubble) gave the interior a lighter 1.5 stroke; at the
    // rail's 16px on a 1x display that lighter detail was the first thing to
    // vanish, which is why that draft was dropped. A second weight creeping back
    // in is the regression this pins.
    expect(svg).not.toMatch(/<path[^>]*stroke-width=/)
    expect((svg.match(/<path /g) ?? []).length).toBe(2)
  })

  it('draws the back ghost as an OPEN partial outline, occluded by the front one', () => {
    // Lucide's `Users` idiom: the figure behind is drawn only where it is not
    // covered, and each cut stops a gap short of the front stroke — that gap is
    // the cue that reads as "behind" rather than "two overlapping outlines".
    // The front ghost is a closed silhouette (ends in Z); the back one must NOT
    // close, or the occlusion is gone and the mark reads as a knot.
    const paths = [...svg.matchAll(/<path d="([^"]+)"/g)].map((m) => m[1].trim())
    expect(paths).toHaveLength(2)
    const [front, back] = paths
    expect(front).toMatch(/Z$/)
    expect(back).not.toMatch(/Z/)
  })

  it('is an outline, not a filled shape', () => {
    // A fill would read as a solid blob under the mask, at a visual weight no
    // stroke width could reconcile with the outlined glyphs beside it.
    expect(svg).toMatch(/fill="none"/)
    expect(svg).not.toMatch(/fill="(?!none)/)
  })

  it('rounds its joins and caps, like every Lucide glyph', () => {
    expect(svg).toMatch(/stroke-linecap="round"/)
    expect(svg).toMatch(/stroke-linejoin="round"/)
  })

  it('carries no width/height, so the mask box governs its size', () => {
    // An intrinsic width/height would fight `mask-size:contain` and pin the art
    // to 24px regardless of the `size` prop.
    expect(svg).not.toMatch(/<svg[^>]*\swidth=/)
    expect(svg).not.toMatch(/<svg[^>]*\sheight=/)
  })
})

describe('Crew Members nav icon', () => {
  it('uses the Crew Member mark', () => {
    const surface = getBuiltinSurface('members')
    expect(surface?.label).toBe('Crew Members')
    expect((surface?.icon as ReactElement).type).toBe(CrewMemberMark)
  })
})
