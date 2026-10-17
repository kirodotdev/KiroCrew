// The composer shows the prompt-length strip for the text a send would post,
// measured against the context window it already receives for the meter.
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

const base = { onChange: vi.fn(), onSend: vi.fn() }

describe('ChatInput prompt-length strip', () => {
  it('stays hidden for an ordinary prompt', () => {
    renderWithProviders(<ChatInput {...base} value="short prompt" contextPct={10} contextWindowTokens={200_000} />)
    expect(screen.queryByTestId('prompt-length-notice')).not.toBeInTheDocument()
  })

  it('does not count outer whitespace stripped before sending', () => {
    const value = `${' '.repeat(1000)}${'x'.repeat(400)}${' '.repeat(1000)}`
    renderWithProviders(<ChatInput {...base} value={value} contextPct={10} contextWindowTokens={100} />)
    expect(screen.getByTestId('prompt-length-notice')).toHaveAttribute('data-level', 'near')
  })

  it('appears once the draft is over the context window', () => {
    renderWithProviders(<ChatInput {...base} value={'x'.repeat(4_400)} contextPct={10} contextWindowTokens={1_000} />)
    expect(screen.getByTestId('prompt-length-notice')).toHaveAttribute('data-level', 'over')
  })
})
