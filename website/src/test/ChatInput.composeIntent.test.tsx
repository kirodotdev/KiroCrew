import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const { emitComposeIntent } = vi.hoisted(() => ({ emitComposeIntent: vi.fn() }))
vi.mock('../lib/composeIntent', () => ({ emitComposeIntent, bindComposeIntentSender: vi.fn() }))

const defaultProps = { value: '', onChange: vi.fn(), onSend: vi.fn() }

/* Composing re-arms a reclaimed session's resume prefetch. Both editor
   branches must reach the sender: the default textarea and the rich-text
   editor that "Style Markdown While Typing" switches on. */
describe('ChatInput compose intent', () => {
  beforeEach(() => emitComposeIntent.mockClear())

  it('the textarea reports focus and keystrokes', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('TEXTAREA')
    fireEvent.focus(input)
    expect(emitComposeIntent).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(input, { key: 'a' })
    expect(emitComposeIntent).toHaveBeenCalledTimes(2)
  })

  it('the rich-text editor reports focus and keystrokes', async () => {
    renderWithProviders(<ChatInput {...defaultProps} lexicalComposer />)
    // LexicalComposerInput loads through React.lazy (chat-input/engine.tsx); a
    // cold import on a loaded worker can outlast the default 1 s query deadline.
    const input = await screen.findByRole('textbox', {}, { timeout: 5000 })
    expect(input).toHaveAttribute('data-lexical-composer')
    emitComposeIntent.mockClear()
    fireEvent.focus(input)
    expect(emitComposeIntent).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(input, { key: 'a' })
    expect(emitComposeIntent).toHaveBeenCalledTimes(2)
  })
})
