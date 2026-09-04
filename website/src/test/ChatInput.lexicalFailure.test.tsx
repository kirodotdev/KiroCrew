import { screen, waitFor } from '@testing-library/react'
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
  })
})
