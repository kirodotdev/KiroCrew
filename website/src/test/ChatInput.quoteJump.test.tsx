import { describe, it, expect, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The quote card staged in the composer (#34335): its text jumps to the
 * quoted message through the host's jump, the same one a sent card uses, and
 * its remove control stays a separate button that only unstages.
 */
describe('ChatInput staged quote card', () => {
  const quote = { role: 'assistant' as const, text: 'Staged files now show as chips.', ts: '2026-09-29T09:12:00Z', mid: 'm9' }

  it('jumps to the quoted message on a click, and remove unstages without jumping', () => {
    const onJumpToQuote = vi.fn()
    const onRemoveQuote = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} pendingQuote={quote} onRemoveQuote={onRemoveQuote} onJumpToQuote={onJumpToQuote} />)
    // Named by the visible quote, so a screen reader still hears what is staged.
    const jump = screen.getByRole('button', { name: /^Quoting Kiro Crew.*Staged files now show as chips\.$/ })
    expect(jump).toHaveAttribute('title', 'Jump to the quoted message')
    fireEvent.click(jump)
    expect(onJumpToQuote).toHaveBeenCalledWith(quote)
    fireEvent.click(screen.getByTestId('quote-card-remove'))
    expect(onRemoveQuote).toHaveBeenCalledTimes(1)
    expect(onJumpToQuote).toHaveBeenCalledTimes(1)
  })

  it('is not a jump control for a quote with no ts to jump to', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} pendingQuote={{ role: 'assistant', text: 'q' }} onRemoveQuote={vi.fn()} onJumpToQuote={vi.fn()} />)
    expect(screen.getByTestId('quote-card-composer')).toBeInTheDocument()
    expect(screen.queryByTestId('quote-card-jump')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Quoting/ })).not.toBeInTheDocument()
  })
})
