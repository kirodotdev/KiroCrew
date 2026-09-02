/**
 * The Kiro ghost brand mark (`components/KiroGhostMark.tsx`) and its use as the
 * "Agent Capabilities" nav icon.
 *
 * Two things are pinned here:
 * - the mark paints the ghost asset as a CSS mask over `currentColor`, which is
 *   what lets it follow the rail's active (accent) / idle colour states instead
 *   of being a fixed-colour <img>;
 * - the `capabilities` built-in surface renders that mark rather than a Lucide
 *   glyph (regression guard for the icon swap).
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ReactElement } from 'react'
import { KiroGhostMark } from '../components/KiroGhostMark'
import '../surfaces/builtins'
import { getBuiltinSurface } from '../surfaces/registry'

describe('KiroGhostMark', () => {
  // The mask contract must be asserted against React's OWN style serialization,
  // not the test DOM: jsdom's `cssstyle` does not implement the `mask-*`
  // longhands, so React's CSSOM assignment is silently dropped and neither
  // `el.style` nor the serialized `style` attribute carries them under jsdom.
  // `renderToStaticMarkup` emits every style key verbatim (React escapes the
  // inline `"` as `&quot;`), independent of the DOM's CSS-property allowlist.
  // DOM-observable props (background-color, size, aria-hidden) are still checked
  // via jsdom, which handles them.
  const maskStyle = () => {
    const html = renderToStaticMarkup(<KiroGhostMark />)
    return /style="([^"]*)"/.exec(html)?.[1] ?? ''
  }

  it('paints the ghost asset as a mask over currentColor', () => {
    const { getByTestId } = render(<KiroGhostMark />)
    // currentColor is what makes the glyph inherit the nav row's text colour.
    expect(getByTestId('kiro-ghost-mark').style.backgroundColor).toBe('currentcolor')
    const style = maskStyle()
    // The ghost SVG (1.2 KB, under the 4 KB inline limit) is inlined as a
    // `data:image/svg+xml,…` URI by the bundler, so the mask paints the ghost
    // art directly. (Vite 5 returned the dev file path — containing the asset
    // filename — in the test transform; Vite 8 inlines it, matching what the
    // production build already shipped. The component's `url={ghostMarkUrl}`
    // binding guarantees it is the ghost asset specifically.)
    expect(style).toContain('data:image/svg+xml') // the ghost asset is the mask source
    expect(style).toContain('mask-size:contain')
  })

  it('quotes the mask URL', () => {
    // Regression guard: Vite inlines the SVG as a `data:image/svg+xml,…` URI
    // whose attributes are single-quoted. An UNQUOTED css `url(…)` token cannot
    // contain quotes, so the browser drops the declaration and the glyph paints
    // as a solid `currentColor` square. React serializes the quotes as `&quot;`.
    expect(maskStyle()).toMatch(/mask-image:url\(&quot;/)
  })

  it('is decorative (hidden from the accessibility tree)', () => {
    const { getByTestId } = render(<KiroGhostMark />)
    expect(getByTestId('kiro-ghost-mark').getAttribute('aria-hidden')).toBe('true')
  })

  it('sizes the box from the size prop', () => {
    const { getByTestId } = render(<KiroGhostMark size={24} />)
    const el = getByTestId('kiro-ghost-mark')
    expect(el.style.width).toBe('24px')
    expect(el.style.height).toBe('24px')
  })
})

describe('Agent Capabilities nav icon', () => {
  it('uses the Kiro ghost mark', () => {
    const surface = getBuiltinSurface('capabilities')
    expect(surface?.label).toBe('Agent Capabilities')
    expect((surface?.icon as ReactElement).type).toBe(KiroGhostMark)
  })
})
