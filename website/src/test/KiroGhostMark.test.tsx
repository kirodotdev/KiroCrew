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
import type { ReactElement } from 'react'
import { KiroGhostMark } from '../components/KiroGhostMark'
import '../surfaces/builtins'
import { getBuiltinSurface } from '../surfaces/registry'

describe('KiroGhostMark', () => {
  // jsdom's cssstyle does not implement `mask-*`, so `style.getPropertyValue`
  // returns '' for them and `-webkit-` prefixed props are dropped entirely.
  // Assert against the serialized style attribute, which does carry them.
  const styleOf = (el: HTMLElement) => el.getAttribute('style') ?? ''

  it('paints the ghost asset as a mask over currentColor', () => {
    const { getByTestId } = render(<KiroGhostMark />)
    const el = getByTestId('kiro-ghost-mark')
    // currentColor is what makes the glyph inherit the nav row's text colour.
    expect(el.style.backgroundColor).toBe('currentcolor')
    expect(styleOf(el)).toContain('kiro-ghost-mark')
    expect(styleOf(el)).toContain('mask-size: contain')
  })

  it('quotes the mask URL', () => {
    // Regression guard: Vite inlines the SVG as a `data:image/svg+xml,…` URI
    // whose attributes are single-quoted. An UNQUOTED css `url(…)` token cannot
    // contain quotes, so the browser drops the declaration and the glyph paints
    // as a solid `currentColor` square (this shipped once and was caught only
    // in a real-browser screenshot).
    const { getByTestId } = render(<KiroGhostMark />)
    expect(styleOf(getByTestId('kiro-ghost-mark'))).toMatch(/mask-image: url\("/)
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
