import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'
import { renderWithProviders } from './helpers'

const props = { value: 'say **hi** now', onChange: vi.fn(), onSend: vi.fn() }

function setInlineMarkdown(on: boolean) {
  saveChatConfig({ ...loadChatConfig(), inlineMarkdown: on })
  window.dispatchEvent(new Event('mc-config-changed'))
}

afterEach(() => {
  localStorage.removeItem('mc-chat-config')
})

// The Style Markdown While Typing setting decides which composer a user gets:
// off keeps the textarea (no styling possible), on switches to Lexical so the
// markdown can be drawn while the stored value stays the typed text.
describe('ChatInput Style Markdown While Typing setting', () => {
  it('keeps the plain textarea with no styling when the setting is off (default)', () => {
    renderWithProviders(<ChatInput {...props} />)
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('TEXTAREA')
    expect(input).toHaveValue('say **hi** now')
    expect(document.querySelector('.font-bold')).toBeNull()
  })

  it('switches to the Lexical composer and styles bold text when the setting is on', async () => {
    setInlineMarkdown(true)
    renderWithProviders(<ChatInput {...props} />)
    const input = await screen.findByRole('textbox')
    expect(input.tagName).toBe('DIV')
    expect(input).toHaveAttribute('data-lexical-composer')
    await waitFor(() => {
      const bold = input.querySelector('.font-bold')
      expect(bold).not.toBeNull()
      expect(bold).toHaveTextContent('hi')
    })
    // Display only: the markers are still in the editor text.
    expect(input).toHaveTextContent('say **hi** now')
  })

  it('follows a live toggle back to the textarea without losing the draft', async () => {
    setInlineMarkdown(true)
    renderWithProviders(<ChatInput {...props} />)
    const input = await screen.findByRole('textbox')
    expect(input.tagName).toBe('DIV')
    act(() => setInlineMarkdown(false))
    await waitFor(() => expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA'))
    expect(screen.getByRole('textbox')).toHaveValue('say **hi** now')
  })
})
