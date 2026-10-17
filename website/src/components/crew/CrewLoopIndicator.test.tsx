import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import CrewLoopIndicator from './CrewLoopIndicator'

describe('CrewLoopIndicator', () => {
  it('renders nothing when off', () => {
    const { container } = render(<CrewLoopIndicator on={false} />)
    expect(container.innerHTML).toBe('')
  })

  it('is decorative: hidden from assistive tech, tooltip kept', () => {
    render(<CrewLoopIndicator on />)
    const el = screen.getByTestId('crew-loop-indicator')
    expect(el.getAttribute('aria-hidden')).toBe('true')
    expect(el.getAttribute('title')).toBe('On watch')
  })

  it('moves only under motion-safe and swaps to a static dot under reduced motion', () => {
    render(<CrewLoopIndicator on />)
    const el = screen.getByTestId('crew-loop-indicator')
    const part = (name: string) => el.querySelector(`[data-part="${name}"]`)!
    // The orbit spins and hides under reduced motion; the ring never moves,
    // so it stays visible either way and is drawn solid enough to read.
    expect(part('orbit').className).toContain('animate-spin')
    expect(part('orbit').className).toContain('motion-reduce:hidden')
    expect(part('ring').className).not.toContain('motion-reduce:hidden')
    expect(part('ring').className).not.toContain('animate')
    expect(part('ring').className).toContain('border-2')
    // The static dot is hidden by default and shown only under reduced motion,
    // and it never animates.
    expect(part('static-dot').className).toMatch(/(^| )hidden( |$)/)
    expect(part('static-dot').className).toContain('motion-reduce:block')
    expect(part('static-dot').className).not.toContain('animate')
    // Accent, never the green "working" presence colour.
    expect(el.innerHTML).not.toContain('bg-ok')
  })
})
