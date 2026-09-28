/**
 * AppIconTile geometry: an app-supplied RASTER icon fills the tile; a glyph
 * does not.
 *
 * The tile draws a rounded plate with a border for every app, and hands the
 * icon a 30px box inside its 44-58px face. That is correct for a glyph — a
 * first-party `/app-assets/` line-art SVG, or the lucide fallback — which is
 * drawn to be read with air around it. It is wrong for an icon FILE, which the
 * publishing guide asks to be a 512x512 opaque square: the file already IS the
 * tile, so inset it reads as a small sticker stuck on a dark plate (the report
 * that prompted this: Endless Worlds' navy-and-gold tile floating in the middle
 * of the Library launchpad's own square).
 *
 * Both halves are pinned here, because the fix is only correct if it is
 * ASYMMETRIC. A change that bleeds every icon would crop a glyph's strokes
 * against the border, and nothing else in the suite would notice.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import AppIcon from '../components/AppIcon'
import AppIconTile from '../components/appstore/AppIconTile'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

/** An installed app's own icon file, served from its install directory. */
const RASTER = '/apps/endless-worlds/art/assets/icon.webp'
/** A first-party themeable glyph — inlined and painted from theme tokens. */
const GLYPH_SVG = '/app-assets/dev-fleet/icon.svg'

function img(): HTMLImageElement {
  return screen.getByRole('presentation', { hidden: true }) as HTMLImageElement
}

describe('AppIconTile geometry', () => {
  it('bleeds a raster icon to the tile edges instead of insetting it', () => {
    render(<AppIconTile name="endless-worlds" iconUrl={RASTER} />)
    const el = img()
    expect(el.getAttribute('src')).toBe(RASTER)
    // Fills the tile: absolutely inset to its edges at 100%, cropping rather
    // than letterboxing an off-spec aspect ratio.
    expect(el.className).toContain('absolute')
    expect(el.className).toContain('inset-0')
    expect(el.className).toContain('w-full')
    expect(el.className).toContain('h-full')
    expect(el.className).toContain('object-cover')
    // No 30px cage, and no radius of its own to disagree with the tile's.
    expect(el.style.width).toBe('')
    expect(el.style.height).toBe('')
    expect(el.className).not.toContain('rounded')
  })

  it('leaves a first-party glyph SVG inset at its own size', () => {
    // The inline path fetches its markup; before that lands it reserves the
    // icon's box, which is the geometry under test either way.
    const { container } = render(<AppIconTile name="dev-fleet" iconUrl={GLYPH_SVG} />)
    const span = container.querySelector('span')
    expect(span).toBeTruthy()
    expect(span!.style.width).toBe('30px')
    expect(span!.style.height).toBe('30px')
    expect(span!.className).not.toContain('object-cover')
    expect(container.querySelector('img')).toBeNull()
  })

  it('leaves a lucide glyph inset — a page icon name is not an icon file', () => {
    const { container } = render(<AppIconTile name="kanban" icon="Shield" />)
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    // lucide renders its size onto the element, so a bled glyph would lose it.
    expect(svg!.getAttribute('width')).toBe('30')
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('AppIcon rasterFill default', () => {
  it('keeps the inset raster geometry when the flag is not passed', () => {
    // Every OTHER call site (the sidebar rail at 16px, the spotlight at 38/56px,
    // the detail page at 64px) draws its own frame around the icon and must be
    // untouched by this change.
    render(<AppIcon iconUrl={RASTER} size={64} />)
    const el = img()
    expect(el.style.width).toBe('64px')
    expect(el.style.height).toBe('64px')
    expect(el.className).toContain('object-contain')
    expect(el.className).not.toContain('absolute')
  })
})
