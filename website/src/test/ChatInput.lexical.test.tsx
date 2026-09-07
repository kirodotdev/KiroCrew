import { fireEvent, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { renderWithProviders } from './helpers'

const paste: PasteBlock = {
  id: 'lexical-seam-paste',
  seq: 1,
  lines: 3,
  content: 'one\ntwo\nthree',
}

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

describe('ChatInput Lexical migration seam', () => {
  it('keeps the established textarea path as the default', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('TEXTAREA')
    expect(input).not.toHaveAttribute('data-lexical-composer')
  })

  it('renders the feature-contained Lexical composer only when explicitly enabled', async () => {
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        value={formatToken(paste)}
        pasteBlocks={[paste]}
        onPasteBlocksChange={vi.fn()}
        lexicalComposer
      />,
    )
    const chip = await screen.findByTestId('paste-token-1')
    expect(chip).toHaveTextContent('Paste #1 · 3 lines')
    expect(chip).not.toHaveTextContent('[ Paste')
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('DIV')
    expect(input).toHaveAttribute('data-lexical-composer')
  })

  it('focuses Lexical on session switch and the global slash shortcut', async () => {
    const { rerender } = renderWithProviders(
      <ChatInput {...defaultProps} lexicalComposer autoFocusKey="A" />,
    )
    const input = await screen.findByRole('textbox')
    await waitFor(() => expect(input).toHaveFocus())
    input.blur()
    rerender(<ChatInput {...defaultProps} lexicalComposer autoFocusKey="B" />)
    await waitFor(() => expect(input).toHaveFocus())
    input.blur()
    fireEvent.keyDown(document.body, { key: '/' })
    expect(input).toHaveFocus()
  })

  it('restores a dictation caret through the Lexical selection bridge', async () => {
    const caretRef = createRef<{ start: number; end: number } | null>()
    const pendingRef = createRef<number | null>()
    caretRef.current = { start: 1, end: 1 }
    pendingRef.current = 3
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        value="hello"
        lexicalComposer
        voiceCaretRef={caretRef}
        voicePendingCaretRef={pendingRef}
      />,
    )
    await waitFor(() => expect(pendingRef.current).toBeNull())
    await waitFor(() => expect(caretRef.current).toEqual({ start: 3, end: 3 }))
  })

  it('rolls back to the production textarea without losing the canonical value', async () => {
    const { rerender } = renderWithProviders(
      <ChatInput {...defaultProps} value="kept draft" lexicalComposer />,
    )
    expect(await screen.findByRole('textbox')).toHaveAttribute('data-lexical-composer')
    rerender(<ChatInput {...defaultProps} value="kept draft" lexicalComposer={false} />)
    const textarea = screen.getByRole('textbox')
    expect(textarea.tagName).toBe('TEXTAREA')
    expect(textarea).toHaveValue('kept draft')
  })
})
