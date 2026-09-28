/**
 * MobileNavGlyph — the glyph inside the mobile topbar nav toggle.
 *
 * The toggle is the ONLY route to the nav rail on a narrow viewport, and it
 * used to render a bare network-fetched <img alt="" aria-hidden> of the
 * product logo: when that asset 404'd (e.g. `/logo.png` unrouted on a proxied
 * serving path) or the fetch hung, the button rendered NOTHING — invisible
 * but still clickable, which is exactly the field report this guards against.
 * The contract under test: a visible glyph exists at EVERY instant — the Menu
 * hamburger before the logo has proven it loads, the logo after its `load`
 * event, the hamburger again on `error` or when a branding swap introduces an
 * asset that has not loaded yet.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import { MobileNavGlyph } from '../App'

afterEach(cleanup)

const glyphOf = (container: HTMLElement) => ({
  fallback: container.querySelector('[data-testid="mobile-nav-fallback"]'),
  img: container.querySelector('img'),
})

describe('MobileNavGlyph', () => {
  it('shows the hamburger fallback until the logo image actually loads', () => {
    const { container } = render(<MobileNavGlyph avatar="/logo.png" />)
    const { fallback, img } = glyphOf(container)
    // Pre-load: fallback visible, img mounted (so the fetch happens) but hidden.
    expect(fallback).not.toBeNull()
    expect(img).not.toBeNull()
    expect(img!.className).toContain('hidden')
  })

  it('swaps to the logo once the image fires load', () => {
    const { container } = render(<MobileNavGlyph avatar="/logo.png" />)
    fireEvent.load(container.querySelector('img')!)
    const { fallback, img } = glyphOf(container)
    expect(fallback).toBeNull()
    expect(img!.className).not.toContain('hidden')
  })

  it('returns to the hamburger when the image errors — never an invisible toggle', () => {
    const { container } = render(<MobileNavGlyph avatar="/logo.png" />)
    const img = container.querySelector('img')!
    fireEvent.load(img)
    fireEvent.error(img)
    const { fallback } = glyphOf(container)
    expect(fallback).not.toBeNull()
  })

  it('treats a branding swap as unproven: hamburger until the NEW src loads', () => {
    const { container, rerender } = render(<MobileNavGlyph avatar="/logo.png" />)
    fireEvent.load(container.querySelector('img')!)
    expect(glyphOf(container).fallback).toBeNull()
    // Theme/branding change swaps the asset; the old load must not vouch for it.
    rerender(<MobileNavGlyph avatar="/other-logo.png" />)
    expect(glyphOf(container).fallback).not.toBeNull()
    fireEvent.load(container.querySelector('img')!)
    expect(glyphOf(container).fallback).toBeNull()
  })

  it('renders only the hamburger when no avatar is configured', () => {
    const { container } = render(<MobileNavGlyph avatar="" />)
    const { fallback, img } = glyphOf(container)
    expect(fallback).not.toBeNull()
    expect(img).toBeNull()
  })
})
