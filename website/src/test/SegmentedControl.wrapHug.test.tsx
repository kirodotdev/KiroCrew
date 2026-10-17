/**
 * `wrap="hug"`: segments keep their content width and the group is no wider
 * than they are, so a row that fits stays ONE row and only a genuinely narrow
 * container wraps. `wrap` (true) keeps its stretch-to-fill behaviour, which the
 * tag manager's policy picker relies on.
 *
 * happy-dom computes no layout, so the contract is pinned on the classes that
 * produce it (the same approach as SegmentedControl.indicatorLayout). The
 * Telemetry panel's 1280px still in the PR shows the result in a real browser.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import SegmentedControl from '../components/SegmentedControl'

const SEGMENTS = ['Default', '24h', '7d', '14d', '30d', '90d', 'Custom'].map(label => ({ key: label, label }))
const classes = (el: Element) => el.className.split(/\s+/)

describe('SegmentedControl wrap modes', () => {
  afterEach(() => cleanup())

  it('hug: content-sized segments, wrapping only when the row does not fit', () => {
    render(<SegmentedControl segments={SEGMENTS} value="Default" onChange={vi.fn()} collapse={false} wrap="hug" />)
    const group = classes(screen.getByRole('radiogroup'))
    expect(group).toContain('flex-wrap')
    expect(group).toContain('max-w-full')
    // Stretching the group to its container is what spread six segments over a
    // 1280px row and pushed the seventh onto a second one.
    expect(group).not.toContain('w-full')
    for (const radio of screen.getAllByRole('radio')) {
      const c = classes(radio)
      expect(c).not.toContain('flex-1')
      expect(c.some(x => x.startsWith('basis-'))).toBe(false)
    }
  })

  it('true: still stretches to fill each row', () => {
    render(<SegmentedControl segments={SEGMENTS} value="Default" onChange={vi.fn()} collapse={false} wrap />)
    expect(classes(screen.getByRole('radiogroup'))).toContain('w-full')
    for (const radio of screen.getAllByRole('radio')) expect(classes(radio)).toContain('flex-1')
  })
})
