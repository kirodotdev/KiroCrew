/**
 * Test: the composer's staged-session-reference strip.
 *
 * Asserts BEHAVIOUR the user can observe — a chip per staged reference, the
 * title and count shown, removal wired to the right key — rather than the class
 * names used to draw it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SessionRefStrip from '../components/SessionRefStrip'
import type { SessionRef } from '../utils/sessionRefs'

const ref = (key: string, title: string, messages?: number): SessionRef => ({ key, title, messages })

describe('SessionRefStrip', () => {
  it('renders nothing at all when no reference is staged', () => {
    const { container } = render(<SessionRefStrip refs={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one chip per staged reference, in staging order', () => {
    render(<SessionRefStrip refs={[ref('a', 'Release notes'), ref('b', 'Auth refactor')]} />)
    const chips = screen.getAllByTestId('session-ref-chip')
    expect(chips).toHaveLength(2)
    expect(chips[0]).toHaveAttribute('data-session-ref', 'a')
    expect(chips[1]).toHaveAttribute('data-session-ref', 'b')
    expect(screen.getByText('Release notes')).toBeTruthy()
    expect(screen.getByText('Auth refactor')).toBeTruthy()
  })

  it('shows the message count when known and omits it entirely when not', () => {
    const { rerender } = render(<SessionRefStrip refs={[ref('a', 'With count', 137)]} />)
    expect(screen.getByText(/137/)).toBeTruthy()
    rerender(<SessionRefStrip refs={[ref('a', 'No count')]} />)
    expect(screen.queryByText(/137/)).toBeNull()
  })

  it('renders a zero count rather than hiding it (0 is known, not missing)', () => {
    render(<SessionRefStrip refs={[ref('a', 'Fresh session', 0)]} />)
    // Anchored on the rendered count element, not a bare /0/ that would also
    // match a zero anywhere else in the chip.
    const chip = screen.getByTestId('session-ref-chip')
    const counts = Array.from(chip.querySelectorAll('span')).map(s => s.textContent ?? '')
    expect(counts.some(t => /\b0\b/.test(t) && t !== 'Fresh session')).toBe(true)
  })

  it('falls back to the session key when the title is empty', () => {
    render(<SessionRefStrip refs={[ref('chat-9-1785440411', '')]} />)
    expect(screen.getByText('chat-9-1785440411')).toBeTruthy()
  })

  it('removes the chip the user clicked, not the first one', async () => {
    const onRemove = vi.fn()
    render(<SessionRefStrip refs={[ref('a', 'First'), ref('b', 'Second')]} onRemove={onRemove} />)
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(2)
    await userEvent.click(buttons[1])
    expect(onRemove).toHaveBeenCalledTimes(1)
    expect(onRemove).toHaveBeenCalledWith('b')
  })

  it('omits the remove control when the strip is read-only', () => {
    render(<SessionRefStrip refs={[ref('a', 'First')]} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('names the target session in the remove control accessible label', () => {
    render(<SessionRefStrip refs={[ref('a', 'Release notes')]} onRemove={vi.fn()} />)
    // Screen-reader users get more than a bare "Remove" repeated per chip.
    const btn = screen.getByRole('button')
    expect(btn.getAttribute('aria-label')).toContain('Release notes')
  })
})
