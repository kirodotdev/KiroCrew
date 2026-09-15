// SegmentedControl responsive collapsing opt-out.
//
// The full -> compact -> dropdown collapse is measured against the PARENT
// element, so a parent whose width comes from this control (shrink-0,
// inline-flex) measures near zero and collapses the control for no reason.
// collapse={false} pins every segment visible.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import SegmentedControl from '../components/SegmentedControl'

const SEGMENTS = [{ key: 'stable', label: 'Stable' }, { key: 'insider', label: 'Insider' }]

describe('SegmentedControl collapse', () => {
  afterEach(cleanup)

  it('keeps every segment clickable in a zero-width parent when collapse=false', async () => {
    const onChange = vi.fn()
    render(
      <div style={{ width: 0 }}>
        <SegmentedControl segments={SEGMENTS} value="stable" onChange={onChange} collapse={false} />
      </div>,
    )
    // Both segments are real buttons from the first paint and stay that way --
    // no toggle to open, so nothing can be occluded by a following sibling.
    await waitFor(() => {
      expect(screen.getAllByRole('button')).toHaveLength(2)
    })
    expect(screen.getByRole('button', { name: /stable/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /insider/i })).toBeTruthy()
  })

  it('still collapses to the dropdown in a narrow parent by default', async () => {
    render(
      <div style={{ width: 0 }}>
        <SegmentedControl segments={SEGMENTS} value="stable" onChange={vi.fn()} />
      </div>,
    )
    // jsdom reports clientWidth 0 for the parent, below the compact threshold:
    // the control renders a single toggle and hides the options behind it.
    await waitFor(() => {
      expect(screen.getAllByRole('button')).toHaveLength(1)
    })
    expect(screen.queryByRole('button', { name: /insider/i })).toBeNull()
  })
})
