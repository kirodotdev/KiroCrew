import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FilterChip } from './SearchFilterBar'

describe('FilterChip gradient', () => {
  it('paints the Crew Mode chip in the crew gradient instead of a single colour', () => {
    render(<FilterChip label="Crew Mode" color="var(--accent)" gradient clearLabel="Clear" onClear={vi.fn()} testId="chip" />)
    const chip = screen.getByTestId('chip')
    expect(chip.className).toContain('crew-grad')
    expect(chip.getAttribute('style')).toBeNull()
  })

  it('a paused Crew Mode chip dims and draws its dash in the gradient\'s own text colour, so the pause is seen', () => {
    // The gradient fill hides a transparent dashed border; a paused chip must
    // still read as "not narrowing right now" outside the open menu.
    render(<FilterChip label="Crew Mode" color="var(--accent)" gradient paused clearLabel="Clear" onClear={vi.fn()} testId="chip" />)
    const chip = screen.getByTestId('chip')
    expect(chip.className).toContain('crew-grad')
    expect(chip.className).toContain('border-dashed')
    expect(chip.className).toContain('border-current')
    expect(chip.className).toContain('opacity-60')
    expect(chip.className).not.toContain('border-transparent')
  })

  it('keeps every other chip on its colour token', () => {
    render(<FilterChip label="Pinned" color="var(--accent)" clearLabel="Clear" onClear={vi.fn()} testId="chip" />)
    expect(screen.getByTestId('chip').className).not.toContain('crew-grad')
  })
})
