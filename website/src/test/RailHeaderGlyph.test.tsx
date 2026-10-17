/**
 * RailHeaderGlyph — the glyph inside the nav-rail header's expand/collapse
 * button.
 *
 * When the rail is collapsed the bot name is unmounted, so this glyph is the
 * button's ONLY visible content. It used to be a bare network-fetched
 * <img alt="" aria-hidden> of the product logo: a 404 on the avatar asset, a
 * blocked request or a hung fetch rendered NOTHING — an invisible control that
 * still toggled the rail when clicked. The contract under test mirrors
 * MobileNavGlyph: a visible glyph exists at EVERY instant — the PanelLeft
 * fallback before the logo has proven it loads, the logo after its `load`
 * event, the fallback again on `error` or when a branding swap introduces an
 * asset that has not loaded yet — and the theme-overridable box class lands on
 * both halves so the swap never moves the button's geometry.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import { RailHeaderGlyph } from '../App'

afterEach(cleanup)

const glyphOf = (container: HTMLElement) => ({
  fallback: container.querySelector('[data-testid="rail-header-fallback"]'),
  img: container.querySelector('img'),
})

const COLLAPSED = 'w-10 h-10'

describe('RailHeaderGlyph', () => {
  it('shows the PanelLeft fallback in the box until the logo image actually loads', () => {
    const { container } = render(<RailHeaderGlyph avatar="/logo.png" boxClass={COLLAPSED} iconSize={24} />)
    const { fallback, img } = glyphOf(container)
    // Pre-load: fallback visible, img mounted (so the fetch happens) but hidden.
    expect(fallback).not.toBeNull()
    expect(fallback!.querySelector('svg')).not.toBeNull()
    expect(img).not.toBeNull()
    expect(img!.className).toContain('hidden')
  })

  it('swaps to the logo once the image fires load', () => {
    const { container } = render(<RailHeaderGlyph avatar="/logo.png" boxClass={COLLAPSED} iconSize={24} />)
    fireEvent.load(container.querySelector('img')!)
    const { fallback, img } = glyphOf(container)
    expect(fallback).toBeNull()
    expect(img!.className).not.toContain('hidden')
  })

  it('returns to the fallback when the image errors — never an invisible control', () => {
    const { container } = render(<RailHeaderGlyph avatar="/logo.png" boxClass={COLLAPSED} iconSize={24} />)
    const img = container.querySelector('img')!
    fireEvent.load(img)
    fireEvent.error(img)
    const { fallback } = glyphOf(container)
    expect(fallback).not.toBeNull()
  })

  it('treats a branding swap as unproven: fallback until the NEW src loads', () => {
    const { container, rerender } = render(<RailHeaderGlyph avatar="/logo.png" boxClass={COLLAPSED} iconSize={24} />)
    fireEvent.load(container.querySelector('img')!)
    expect(glyphOf(container).fallback).toBeNull()
    // Theme/branding change swaps the asset; the old load must not vouch for it.
    rerender(<RailHeaderGlyph avatar="/other-logo.png" boxClass={COLLAPSED} iconSize={24} />)
    expect(glyphOf(container).fallback).not.toBeNull()
    fireEvent.load(container.querySelector('img')!)
    expect(glyphOf(container).fallback).toBeNull()
  })

  it('applies the theme-overridable box class to fallback and logo alike, keeping the hover tilt', () => {
    const { container } = render(<RailHeaderGlyph avatar="/logo.png" boxClass="w-12 h-12" iconSize={24} />)
    const { fallback, img } = glyphOf(container)
    for (const el of [fallback!, img!]) {
      expect(el.className).toContain('w-12 h-12')
      expect(el.className).toContain('transition-all')
      expect(el.className).toContain('group-hover:rotate-[-8deg]')
    }
  })

  it('renders only the fallback when no avatar is configured', () => {
    const { container } = render(<RailHeaderGlyph avatar="" boxClass={COLLAPSED} iconSize={24} />)
    const { fallback, img } = glyphOf(container)
    expect(fallback).not.toBeNull()
    expect(img).toBeNull()
  })
})
