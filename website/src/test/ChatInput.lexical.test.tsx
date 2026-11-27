import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { renderWithProviders } from './helpers'
import { LAZY_COMPOSER_MOUNT } from './lazyComposerReady'
import { clickOptimize } from './optimizeRow'

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
    const chip = await screen.findByTestId('paste-token-1', undefined, LAZY_COMPOSER_MOUNT)
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
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
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
      <ComposerVoiceSliceOverride inputProps={{ voiceCaretRef: caretRef, voicePendingCaretRef: pendingRef }}>
        <ChatInput
          {...defaultProps}
          value="hello"
          lexicalComposer
        />
      </ComposerVoiceSliceOverride>,
    )
    await waitFor(() => expect(pendingRef.current).toBeNull())
    await waitFor(() => expect(caretRef.current).toEqual({ start: 3, end: 3 }))
  })

  it('rolls back to the production textarea without losing the canonical value', async () => {
    const { rerender } = renderWithProviders(
      <ChatInput {...defaultProps} value="kept draft" lexicalComposer />,
    )
    expect(await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)).toHaveAttribute('data-lexical-composer')
    rerender(<ChatInput {...defaultProps} value="kept draft" lexicalComposer={false} />)
    const textarea = screen.getByRole('textbox')
    expect(textarea.tagName).toBe('TEXTAREA')
    expect(textarea).toHaveValue('kept draft')
  })

  it('sends on Enter while streaming dictation keeps Lexical text read-only', async () => {
    const onSend = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{
        voiceRecording: true,
        voiceTransport: 'stream',
        voiceDictationPanel: true,
        voiceSampleRef: { current: { level: 0, onset: 0 } },
      }}>
        <ChatInput {...defaultProps} onSend={onSend} value="live words" lexicalComposer />
      </ComposerVoiceSliceOverride>,
    )
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['ctrlKey', { ctrlKey: true }],
    ['metaKey', { metaKey: true }],
  ])('sends on %s+Enter while streaming dictation keeps Lexical text read-only', async (_name, modifier) => {
    const onSend = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{
        voiceRecording: true,
        voiceTransport: 'stream',
        voiceDictationPanel: true,
        voiceSampleRef: { current: { level: 0, onset: 0 } },
      }}>
        <ChatInput {...defaultProps} onSend={onSend} value="live words" lexicalComposer />
      </ComposerVoiceSliceOverride>,
    )
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    fireEvent.keyDown(input, { key: 'Enter', ...modifier })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('keeps Lexical read-only after an optimize started during streaming dictation is cancelled', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
    try {
      renderWithProviders(
        <ComposerVoiceSliceOverride inputProps={{
          voiceRecording: true,
          voiceTransport: 'stream',
          voiceDictationPanel: true,
          voiceSampleRef: { current: { level: 0, onset: 0 } },
        }}>
          <ChatInput {...defaultProps} value="live words" connected lexicalComposer />
        </ComposerVoiceSliceOverride>,
      )
      const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
      await waitFor(() => expect(root.getAttribute('contenteditable')).toBe('false'))
      clickOptimize()
      const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
      await act(async () => { fireEvent.click(cancel) })
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
      expect(root.getAttribute('contenteditable')).toBe('false')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
