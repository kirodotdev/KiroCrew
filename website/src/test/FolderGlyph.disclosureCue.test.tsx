/**
 * The folder glyph carries a collapse cue.
 *
 * Collapsible callers (the ones that pass `open`) get a small disclosure
 * chevron overlaid on the glyph box — rotated 90° when open, matching the
 * sidebar's one chevron grammar — on both the emoji and the lucide
 * Folder/FolderOpen shape (#11269: testers did not read the shape swap as
 * state). Non-collapse callers (modal preview, menus) that omit `open` render
 * the bare glyph. The overlay is absolutely positioned so the glyph box width
 * never changes and the folder-alignment geometry stays untouched, and it sits
 * in a notch masked out of the shape so it never crosses the folder outline.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import FolderGlyph from '../components/FolderGlyph'

describe('FolderGlyph disclosure cue', () => {
  it('overlays a chevron when a collapsible caller passes open', () => {
    const { getByTestId } = render(
      <FolderGlyph icon="🚀" size={14} open={false} testId="g" />,
    )
    const chevron = getByTestId('g-disclosure')
    expect(chevron).toBeTruthy()
    expect(chevron.classList.contains('rotate-90')).toBe(false)
    // Out of flow: the glyph box keeps its exact width (alignment geometry).
    expect(chevron.classList.contains('absolute')).toBe(true)
  })

  it('rotates the chevron when the folder is expanded', () => {
    const { getByTestId } = render(
      <FolderGlyph icon="🚀" size={14} open={true} testId="g" />,
    )
    expect(getByTestId('g-disclosure').classList.contains('rotate-90')).toBe(true)
  })

  it('renders the bare emoji for non-collapse callers that omit open', () => {
    const { getByTestId, queryByTestId } = render(
      <FolderGlyph icon="🚀" size={20} testId="g" />,
    )
    expect(getByTestId('g').textContent).toBe('🚀')
    expect(queryByTestId('g-disclosure')).toBeNull()
  })

  it('overlays the same chevron on the lucide glyph when collapsed', () => {
    const { getByTestId } = render(
      <FolderGlyph size={12} open={false} testId="g" />,
    )
    const chevron = getByTestId('g-disclosure')
    expect(chevron.classList.contains('rotate-90')).toBe(false)
    expect(chevron.classList.contains('absolute')).toBe(true)
  })

  it('rotates the lucide glyph chevron when expanded', () => {
    const { getByTestId } = render(
      <FolderGlyph size={12} open={true} testId="g" />,
    )
    expect(getByTestId('g-disclosure').classList.contains('rotate-90')).toBe(true)
  })

  it('renders the bare lucide glyph for callers that omit open', () => {
    const { getByTestId, queryByTestId } = render(
      <FolderGlyph size={20} testId="g" />,
    )
    expect(getByTestId('g').querySelector('svg')).toBeTruthy()
    expect(queryByTestId('g-disclosure')).toBeNull()
    // No chevron, no notch: the bare glyph is never masked.
    expect(getByTestId('g-shape').getAttribute('style') ?? '').not.toMatch(/mask/)
  })

  // The chevron used to sit on top of the folder outline and hang past the box
  // into the name's gap, reading as a smudge. It now sits in a hole cut out of
  // the shape, centred inside the glyph box.
  it.each([
    ['lucide', undefined],
    ['emoji', '🚀'],
  ])('cuts a notch in the %s shape under the chevron', (_kind, icon) => {
    const size = 12
    const { getByTestId } = render(
      <FolderGlyph icon={icon} size={size} open={false} testId="g" />,
    )
    const mask = getByTestId('g-shape').style.maskImage
    expect(mask).toMatch(/^radial-gradient\(circle at ([\d.]+)px \1px, transparent/)
    const centre = Number(mask.match(/at ([\d.]+)px/)![1])
    const chevron = getByTestId('g-disclosure')
    const width = Number(chevron.getAttribute('width'))
    const left = parseFloat(chevron.style.left)
    // The chevron is centred on the notch...
    expect(left + width / 2).toBeCloseTo(centre)
    // ...and the notch centre is inside the box, so lucide's chevron ink (a
    // quarter of the icon size either side of centre) stays within it.
    expect(centre + width / 4).toBeLessThanOrEqual(size)
  })
})
