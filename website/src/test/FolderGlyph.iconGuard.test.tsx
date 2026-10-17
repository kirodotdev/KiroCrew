/**
 * A malformed persisted icon must never take the sidebar down.
 *
 * folders.json is a hand-editable file on disk, so `icon` is boundary input
 * at render time even though every server write path coerces it to a string.
 * Rendering a non-string value as a React child throws ("Objects are not
 * valid as a React child") and the whole sidebar fails to mount. FolderGlyph
 * therefore accepts only a non-empty string as an emoji icon; every other
 * shape falls back to the default lucide glyph — visible, recoverable, and
 * identical to a folder that never had an icon.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import FolderGlyph from '../components/FolderGlyph'

// The default (lucide) branch renders an <svg>; the emoji branch renders text.
const rendersDefaultGlyph = (el: HTMLElement) =>
  el.querySelector('svg') !== null

describe('FolderGlyph rejects non-string icons instead of crashing', () => {
  it.each([
    ['an object', {}],
    ['an array', ['🚀']],
    ['a number', 1],
    ['a boolean', true],
  ])('renders the default glyph for %s', (_label, bad) => {
    const { getByTestId } = render(
      // Deliberately violates the prop type: this is exactly the shape a
      // hand-corrupted folders.json delivers through the untyped JSON path.
      <FolderGlyph icon={bad as unknown as string} size={14} open={false} testId="g" />,
    )
    const glyph = getByTestId('g')
    expect(rendersDefaultGlyph(glyph)).toBe(true)
  })

  it('renders the default glyph for an empty string (no icon set)', () => {
    const { getByTestId } = render(<FolderGlyph icon="" size={14} testId="g" />)
    expect(rendersDefaultGlyph(getByTestId('g'))).toBe(true)
  })

  it('still renders a genuine emoji string as the emoji branch', () => {
    const { getByTestId } = render(<FolderGlyph icon="🚀" size={14} testId="g" />)
    const glyph = getByTestId('g')
    expect(rendersDefaultGlyph(glyph)).toBe(false)
    expect(glyph.textContent).toContain('🚀')
  })
})
