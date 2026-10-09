/**
 * The emoji folder glyph keeps a collapse cue.
 *
 * The lucide Folder/FolderOpen pair carries the collapsed/expanded state by
 * shape; an emoji cannot. Collapsible callers (the ones that pass `open`)
 * therefore get a small disclosure chevron overlaid on the emoji box —
 * rotated 90° when open, matching the sidebar's one chevron grammar — while
 * non-collapse callers (modal preview, menus) that omit `open` render the
 * bare emoji. The overlay is absolutely positioned so the glyph box width
 * never changes and the folder-alignment geometry stays untouched.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import FolderGlyph from '../components/FolderGlyph'

describe('FolderGlyph disclosure cue on emoji folders', () => {
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

  it('keeps the lucide shape pair for folders without an icon', () => {
    const { getByTestId } = render(
      <FolderGlyph size={14} open={false} testId="g" />,
    )
    // No chevron overlay on the default glyph — Folder vs FolderOpen is the cue.
    expect(getByTestId('g').querySelector('svg')).toBeTruthy()
    expect(getByTestId('g').querySelector('[data-testid="g-disclosure"]')).toBeNull()
  })
})
