/**
 * `compact` is the caller's own decision, taken where the control's measured
 * collapse cannot help: the measurement reads the PARENT's width, and a parent
 * that hugs its content reports this control's own width back — always "plenty
 * of room", at every viewport. So a row that has to fit a phone needs a way to
 * say so directly.
 *
 * jsdom does no layout, so the parent width the measurement reads is stubbed;
 * without that stub every case here would collapse to the dropdown and the
 * distinction under test would not exist.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import SegmentedControl from '../components/SegmentedControl'

const SEGMENTS = [
  { key: 'grid', label: 'Gallery' },
  { key: 'table', label: 'Table' },
]

/** A parent wide enough that the measured collapse would choose `full`. */
function stubRoomyParent() {
  vi.spyOn(window.HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(400)
}

describe('SegmentedControl compact', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('hides the unselected label even when the parent measures roomy', async () => {
    stubRoomyParent()
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} compact />)
    await waitFor(() => expect(screen.getAllByRole('button')).toHaveLength(2))
    // Selected keeps its label — the current state must stay readable.
    expect(screen.getByRole('button', { name: /gallery/i }).textContent).toContain('Gallery')
    expect(screen.getByRole('button', { name: /table/i }).textContent).toBe('')
  })

  it('stays a real two-button control in a parent that measures zero', async () => {
    // No width stub: jsdom reports 0, which is below the dropdown threshold.
    // Compact trades labels for width; it must NOT also trade away the one-tap
    // switch, which is what the dropdown form costs.
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} compact />)
    await waitFor(() => expect(screen.getAllByRole('button')).toHaveLength(2))
  })

  it('keeps both labels without it, in the same roomy parent', async () => {
    stubRoomyParent()
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} />)
    await waitFor(() => expect(screen.getAllByRole('button')).toHaveLength(2))
    expect(screen.getByRole('button', { name: /table/i }).textContent).toContain('Table')
  })

  it('names the icon-only segment explicitly, not via its tooltip', async () => {
    stubRoomyParent()
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} compact />)
    await waitFor(() => expect(screen.getAllByRole('button')).toHaveLength(2))
    // `title` is only the accessible-name FALLBACK and never renders on touch,
    // which is the form factor compact exists for — the hidden label has to be
    // carried by aria-label.
    expect(screen.getByRole('button', { name: /table/i }).getAttribute('aria-label')).toBe('Table')
    // The selected segment still shows its label, so naming it again would be
    // a duplicate the screen reader has to sit through.
    expect(screen.getByRole('button', { name: /gallery/i }).getAttribute('aria-label')).toBeNull()
  })
})
