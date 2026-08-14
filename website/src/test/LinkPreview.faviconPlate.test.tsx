/**
 * The favicon plate: a site that ships one tab-coloured icon must stay visible
 * on a theme that icon was not drawn for.
 *
 * The measurement itself is unit-tested against real pixel data in
 * `iconContrast.test.ts`; here the two DOM-dependent halves are stubbed, because
 * jsdom neither decodes an image nor applies the stylesheet the surface colour
 * would be read from. What this file pins is the wiring: which element the
 * surface is sampled from, and when the decision is retaken.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import type { IconTone } from '../lib/iconContrast'
import type { LinkMeta } from '../lib/linkMeta'

const measureIconTone = vi.fn<(img: HTMLImageElement) => IconTone | null>()
const surfaceLuminance = vi.fn<(el: Element | null) => number | null>()
const themeListeners = new Set<() => void>()

vi.mock('../lib/iconContrast', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/iconContrast')>()
  return {
    ...real,
    measureIconTone: (img: HTMLImageElement) => measureIconTone(img),
    surfaceLuminance: (el: Element | null) => surfaceLuminance(el),
    subscribeThemeSurface: (l: () => void) => {
      themeListeners.add(l)
      return () => themeListeners.delete(l)
    },
  }
})

const { LinkChip, LinkCard } = await import('../components/LinkPreview')
const { relativeLuminance } = await import('../lib/iconContrast')

const DARK_SURFACE = relativeLuminance(0x12, 0x14, 0x1a)
const LIGHT_SURFACE = relativeLuminance(0xfa, 0xfa, 0xfa)
const DARK_GLYPH: IconTone = { dim: 0.02, bright: 0.03, coverage: 0.4 }
const LIGHT_GLYPH: IconTone = { dim: 0.95, bright: 1, coverage: 0.4 }

const meta: LinkMeta = {
  url: 'https://example.com/post',
  title: 'Example Title',
  description: '',
  siteName: 'Example',
  domain: 'example.com',
  icon: 'data:image/png;base64,AAAA',
  iconDark: '',
  fetchedAt: 1770000000,
}

const DARK_VARIANT = 'data:image/png;base64,DARK'

/** Render, then report the decoded icon and its fixed-size box. */
function renderChip(over: Partial<LinkMeta> = {}) {
  const { container } = render(<LinkChip meta={{ ...meta, ...over }} href={meta.url} />)
  const img = container.querySelector('img')!
  act(() => { fireEvent.load(img) })
  return {
    container,
    box: container.querySelector('[aria-hidden="true"]')!,
    src: () => container.querySelector('img')?.getAttribute('src'),
  }
}

beforeEach(() => {
  measureIconTone.mockReset()
  surfaceLuminance.mockReset()
  themeListeners.clear()
})
afterEach(() => { vi.unstubAllGlobals() })

describe('favicon plate', () => {
  it('plates a dark glyph that would vanish into a dark surface', () => {
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    expect(renderChip().box.className).toContain('bg-text')
  })

  it('paints nothing behind an icon that already contrasts', () => {
    // The default has to stay "no background": favicons are routinely
    // transparent, and an unnecessary plate reads as a square the site never
    // shipped.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    expect(renderChip().box.className).not.toContain('bg-text')
  })

  it('plates in the other direction too, for a light glyph on a light surface', () => {
    measureIconTone.mockReturnValue(LIGHT_GLYPH)
    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    // `--text` is the token every theme guarantees against its own backgrounds,
    // so the SAME class is correct in both directions — dark here, light above.
    expect(renderChip().box.className).toContain('bg-text')
  })

  it('samples the surface from the box PARENT, never the box itself', () => {
    // Once plated, the box's own background IS the plate. Measuring the box
    // would compare the icon against the plate, find plenty of contrast, and
    // drop it again — an oscillation, or a plate that flickers off on any theme
    // event.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { box } = renderChip()
    expect(surfaceLuminance).toHaveBeenCalled()
    for (const [arg] of surfaceLuminance.mock.calls) {
      expect(arg).not.toBe(box)
      expect(arg).toBe(box.parentElement)
    }
  })

  it('retakes the decision when the theme changes', () => {
    // A transcript is long-lived: switching to a light theme must drop a plate
    // that was only needed on the dark one, without waiting for a re-render.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { box } = renderChip()
    expect(box.className).toContain('bg-text')

    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    act(() => { themeListeners.forEach((l) => l()) })
    expect(box.className).not.toContain('bg-text')
  })

  it('stops listening for theme changes once unmounted', () => {
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { container } = render(<LinkChip meta={meta} href={meta.url} />)
    act(() => { fireEvent.load(container.querySelector('img')!) })
    expect(themeListeners.size).toBe(1)
    // A scrolled-away chip must not keep a live subscription to the palette.
    act(() => { render(<span />, { container }) })
    expect(themeListeners.size).toBe(0)
  })

  it('drops the plate with the icon when the image fails to load', () => {
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { container, box } = renderChip()
    expect(box.className).toContain('bg-text')
    // The fallback globe is a themed glyph with its own contrast, so a plate
    // behind it would be a filled square around an icon that never needed one.
    act(() => { fireEvent.error(container.querySelector('img')!) })
    expect(container.querySelector('[aria-hidden="true"]')!.className).not.toContain('bg-text')
  })

  it('does not plate when the icon cannot be measured', () => {
    // No canvas backend, an undecodable icon: unknown means untouched.
    measureIconTone.mockReturnValue(null)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    expect(renderChip().box.className).not.toContain('bg-text')
  })

  it('applies to the card icon on the same terms', () => {
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { container } = render(<LinkCard meta={meta} href={meta.url} />)
    act(() => { fireEvent.load(container.querySelector('img')!) })
    expect(container.querySelector('[aria-hidden="true"]')!.className).toContain('bg-text')
  })

  it('keeps the plate out of the accessible name and the sizing classes', () => {
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const { box } = renderChip()
    // The plate is decoration on an aria-hidden box, and the reserved size is
    // unchanged, so a plated icon cannot reflow the sentence around it.
    expect(box.getAttribute('aria-hidden')).toBe('true')
    expect(box.className).toContain('w-[14px]')
    expect(box.className).toContain('h-[14px]')
    // Themed token, not a literal colour.
    expect(box.className).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(box.className).not.toMatch(/rgba?\(/)
  })
})

describe('declared dark icon variant', () => {
  it('renders the site\u2019s own dark icon on a dark surface, and no plate', () => {
    // The icon its designer drew for this case beats anything inferred here, so
    // when the site ships one the plate is not needed at all.
    measureIconTone.mockReturnValue(LIGHT_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: DARK_VARIANT })
    expect(chip.src()).toBe(DARK_VARIANT)
    expect(chip.box.className).not.toContain('bg-text')
  })

  it('keeps the default icon on a light surface', () => {
    // A dark-scheme icon on a light surface is the same invisible-glyph bug in
    // mirror image.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    expect(renderChip({ iconDark: DARK_VARIANT }).src()).toBe(meta.icon)
  })

  it('keeps the default icon when the surface cannot be measured', () => {
    // Unknown is not dark: without a measurement the site's default icon is the
    // only defensible choice.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(null)
    expect(renderChip({ iconDark: DARK_VARIANT }).src()).toBe(meta.icon)
  })

  it('swaps the variant when the theme changes', () => {
    measureIconTone.mockReturnValue(LIGHT_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: DARK_VARIANT })
    expect(chip.src()).toBe(DARK_VARIANT)

    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    act(() => { themeListeners.forEach((l) => l()) })
    expect(chip.src()).toBe(meta.icon)
  })

  it('re-measures after a variant swap instead of reusing the old tone', () => {
    // The two variants are different pictures. Carrying the first one's tone
    // over would decide the plate from an icon that is no longer on screen.
    measureIconTone.mockReturnValue(LIGHT_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: DARK_VARIANT })
    measureIconTone.mockClear()

    surfaceLuminance.mockReturnValue(LIGHT_SURFACE)
    act(() => { themeListeners.forEach((l) => l()) })
    act(() => { fireEvent.load(chip.container.querySelector('img')!) })

    expect(measureIconTone).toHaveBeenCalled()
    expect(measureIconTone.mock.calls[0][0].getAttribute('src')).toBe(meta.icon)
  })

  it('still plates when the site ships no variant', () => {
    // Nothing about the variant path may weaken the fallback: the reported bug
    // is a site with exactly one icon.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: '' })
    expect(chip.src()).toBe(meta.icon)
    expect(chip.box.className).toContain('bg-text')
  })

  it('demotes an undecodable variant to the default icon, not to the placeholder', () => {
    // The backend validates an icon's content-type header, not its magic bytes,
    // so a variant can arrive as a 200 that no decoder accepts. Losing the whole
    // favicon over the nicety would be worse than never having sent it.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: DARK_VARIANT })
    expect(chip.src()).toBe(DARK_VARIANT)

    act(() => { fireEvent.error(chip.container.querySelector('img')!) })
    expect(chip.src()).toBe(meta.icon)
    // And the surviving icon is still measured, so it gets the plate it needs.
    act(() => { fireEvent.load(chip.container.querySelector('img')!) })
    expect(chip.container.querySelector('[aria-hidden="true"]')!.className).toContain('bg-text')
  })

  it('falls back to the placeholder only once BOTH icons have failed', () => {
    // Keying failure by src rather than by a single flag is also what stops the
    // choice bouncing between two dead images forever.
    measureIconTone.mockReturnValue(DARK_GLYPH)
    surfaceLuminance.mockReturnValue(DARK_SURFACE)
    const chip = renderChip({ iconDark: DARK_VARIANT })
    act(() => { fireEvent.error(chip.container.querySelector('img')!) })
    act(() => { fireEvent.error(chip.container.querySelector('img')!) })
    expect(chip.container.querySelector('img')).toBeNull()
    expect(chip.container.querySelector('[aria-hidden="true"]')).not.toBeNull()
  })
})
