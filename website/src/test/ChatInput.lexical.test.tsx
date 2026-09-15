import { fireEvent, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
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
    expect(chip).toHaveTextContent(/3 lines/) // first-line snippet + count
    expect(chip).not.toHaveTextContent('[ Paste')
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('DIV')
    expect(input).toHaveAttribute('data-lexical-composer')
  })

  it('keeps a Lexical draft intact when Enter is pressed offline', async () => {
    const onSend = vi.fn()

    function Host() {
      const [value, setValue] = useState('offline draft')
      return (
        <>
          <ChatInput
            value={value}
            onChange={setValue}
            onSend={() => { onSend(); setValue('') }}
            lexicalComposer
            connected={false}
          />
          <output data-testid="host-value">{value}</output>
        </>
      )
    }

    renderWithProviders(<Host />)
    const editor = await screen.findByRole('textbox')
    await waitFor(() => expect(editor).toHaveAttribute('data-lexical-composer'))
    fireEvent.keyDown(editor, { key: 'Enter', code: 'Enter' })

    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByTestId('host-value')).toHaveTextContent('offline draft')
  })

  it('sends a Lexical draft on Enter when connected', async () => {
    const onSend = vi.fn()

    function Host() {
      const [value, setValue] = useState('online draft')
      return (
        <ChatInput
          value={value}
          onChange={setValue}
          onSend={onSend}
          lexicalComposer
          connected
        />
      )
    }

    renderWithProviders(<Host />)
    const editor = await screen.findByRole('textbox')
    await waitFor(() => expect(editor).toHaveAttribute('data-lexical-composer'))
    fireEvent.keyDown(editor, { key: 'Enter', code: 'Enter' })

    expect(onSend).toHaveBeenCalledTimes(1)
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

  it('selecting a slash command after + → / is undoable back to the draft', async () => {
    const command = { name: '/aa', description: 'Alpha command' }
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((url) => {
      if (typeof url === 'string' && url.includes('/api/slash-commands')) {
        return Promise.resolve(new Response(JSON.stringify([command]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }))
      }
      return Promise.resolve(new Response('[]', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })

    function Host() {
      const [value, setValue] = useState('my draft')
      return (
        <>
          <ChatInput
            value={value}
            onChange={setValue}
            onSend={vi.fn()}
            onUploadFiles={vi.fn()}
            lexicalComposer
            connected
          />
          <output data-testid="host-value">{value}</output>
        </>
      )
    }

    try {
      renderWithProviders(<Host />)
      const editor = await screen.findByRole('textbox')
      await waitFor(() => expect(editor).toHaveAttribute('data-lexical-composer'))
      fireEvent.click(screen.getByTitle('Add files & options'))
      fireEvent.click(screen.getByTitle('Slash commands'))
      await waitFor(() => expect(screen.getByTestId('host-value')).toHaveTextContent('/'))
      fireEvent.mouseDown(await screen.findByRole('option', { name: /\/aa/ }))
      await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe('/aa '))

      fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
      await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe('/'))
      fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
      await waitFor(() => expect(screen.getByTestId('host-value')).toHaveTextContent('my draft'))
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('makes a host-side quote-to-compose update undoable', async () => {
    function Host() {
      const [value, setValue] = useState('my draft')
      return (
        <>
          <ChatInput value={value} onChange={setValue} onSend={vi.fn()} lexicalComposer connected />
          <button type="button" onClick={() => setValue(current => `${current}\n> quoted`)}>Quote</button>
          <output data-testid="host-value">{value}</output>
        </>
      )
    }

    renderWithProviders(<Host />)
    const editor = await screen.findByRole('textbox')
    await waitFor(() => expect(editor).toHaveAttribute('data-lexical-composer'))
    fireEvent.click(screen.getByRole('button', { name: 'Quote' }))
    await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe('my draft\n> quoted'))
    fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
    await waitFor(() => expect(screen.getByTestId('host-value')).toHaveTextContent('my draft'))
  })

  it('makes a host-side send clear undoable', async () => {
    function Host() {
      const [value, setValue] = useState('sent text')
      return (
        <>
          <ChatInput value={value} onChange={setValue} onSend={vi.fn()} lexicalComposer connected />
          <button type="button" onClick={() => setValue('')}>Clear after send</button>
          <output data-testid="host-value">{value}</output>
        </>
      )
    }

    renderWithProviders(<Host />)
    const editor = await screen.findByRole('textbox')
    await waitFor(() => expect(editor).toHaveAttribute('data-lexical-composer'))
    fireEvent.click(screen.getByRole('button', { name: 'Clear after send' }))
    await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe(''))
    fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
    await waitFor(() => expect(screen.getByTestId('host-value')).toHaveTextContent('sent text'))
  })

  it('makes optimizer write-back undoable with paste blocks at the editor and host', async () => {
    const original = `Draft ${formatToken(paste)}`
    const optimized = `Optimized ${formatToken(paste)}`
    const onHostChange = vi.fn()
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((url) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve(new Response(
          JSON.stringify({ changed: true, optimized }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
      }
      return Promise.resolve(new Response('[]', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })
    function Host() {
      const [value, setValue] = useState(original)
      const [blocks, setBlocks] = useState([paste])
      return (
        <>
          <ChatInput
            value={value}
            onChange={(next) => { onHostChange(next); setValue(next) }}
            onSend={vi.fn()}
            pasteBlocks={blocks}
            onPasteBlocksChange={setBlocks}
            lexicalComposer
            connected
          />
          <output data-testid="host-value">{value}</output>
          <output data-testid="host-blocks">{JSON.stringify(blocks)}</output>
        </>
      )
    }

    try {
      renderWithProviders(<Host />)
      const editor = await screen.findByRole('textbox')
      expect(await screen.findByTestId('paste-token-1')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

      await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe(optimized))
      expect(onHostChange).toHaveBeenCalledWith(optimized)
      expect(JSON.parse(screen.getByTestId('host-blocks').textContent!)).toEqual([paste])

      fireEvent.keyDown(editor, { key: 'z', ctrlKey: true })
      await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe(original))
      expect(JSON.parse(screen.getByTestId('host-blocks').textContent!)).toEqual([paste])

      fireEvent.keyDown(editor, { key: 'z', ctrlKey: true, shiftKey: true })
      await waitFor(() => expect(screen.getByTestId('host-value').textContent).toBe(optimized))
      expect(JSON.parse(screen.getByTestId('host-blocks').textContent!)).toEqual([paste])
    } finally {
      fetchSpy.mockRestore()
    }
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
