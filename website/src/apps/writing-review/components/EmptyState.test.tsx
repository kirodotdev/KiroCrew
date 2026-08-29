/**
 * Contract test for ``EmptyState``.
 *
 * The empty-state placeholder is what the writing-review detail pane
 * renders when no review is selected and no scan is in flight. Two
 * behaviours pinned here:
 *
 * 1. Both the title and the hint copy render as non-empty text under
 *    the ``i18nT`` seams so a locale swap replaces the visible words
 *    without breaking the layout.
 * 2. The pen icon carries ``aria-hidden`` so a screen reader announces
 *    only the title/hint pair, not "icon".
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import EmptyState from './EmptyState'

describe('EmptyState', () => {
  it('renders non-empty title and hint copy on both text lines', () => {
    const { container } = render(<EmptyState />)
    const textLines = container.querySelectorAll('div > div')
    // Two ``<div>`` children under the outer wrapper: the title line and
    // the hint line. Both MUST carry non-empty text — a hollow placeholder
    // would leave a screen reader silent and the visual layout hollow.
    expect(textLines.length).toBeGreaterThanOrEqual(2)
    for (const textLine of textLines) {
      expect((textLine.textContent ?? '').trim().length).toBeGreaterThan(0)
    }
  })

  it('marks the decorative icon aria-hidden for screen readers', () => {
    const { container } = render(<EmptyState />)
    const iconElement = container.querySelector('svg')
    expect(iconElement).not.toBeNull()
    expect(iconElement).toHaveAttribute('aria-hidden', 'true')
  })
})
