import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from './helpers'

vi.mock('../components/LexicalComposerInput', () => {
  throw new Error('simulated Lexical chunk failure')
})

const { default: ChatInput } = await import('../components/ChatInput')

describe('ChatInput Lexical lazy-load recovery', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeAll(() => {
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterAll(() => {
    consoleError.mockRestore()
  })

  it('shows a busy status, then restores the textarea with the draft intact', async () => {
    renderWithProviders(
      <ChatInput
        value="recover me"
        onChange={vi.fn()}
        onSend={vi.fn()}
        lexicalComposer
      />,
    )
    expect(screen.getByRole('status', { name: 'Message input' })).toHaveAttribute('aria-busy', 'true')
    const textarea = await screen.findByRole('textbox', { name: 'Message input' })
    await waitFor(() => expect(textarea.tagName).toBe('TEXTAREA'))
    expect(textarea).toHaveValue('recover me')
    // Seamless for typing, but never silent: the person who opted into the
    // editor is told they are no longer in it, and can dismiss the notice.
    const notice = screen.getByTestId('composer-fallback-notice')
    expect(notice).toHaveTextContent('The upgraded editor could not load, so the standard input is active. Your draft is unchanged.')
    fireEvent.click(within(notice).getByRole('button'))
    expect(screen.queryByTestId('composer-fallback-notice')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveValue('recover me')
    // And the operator gets the diagnosable failure: the boundary must log
    // the chunk error so a broken deploy is visible in telemetry.
    expect(consoleError.mock.calls.some(call =>
      typeof call[0] === 'string' && call[0].includes('Lexical composer failed to load'),
    )).toBe(true)
  })
})
