import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * fireEvent.paste passes eventProperties into the native event's clipboardData,
 * but jsdom's DataTransferItemList doesn't support our custom items array.
 * Instead we rely on the fact that React's SyntheticEvent reads from the native
 * event's clipboardData. We set `types` (which jsdom respects) and for the
 * file-upload path we verify the guard logic via the negative tests.
 */

describe('ChatInput paste: prefer text over image', () => {
  it('does NOT upload files when clipboard has text/plain alongside image (macOS Office copy)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    // Simulate macOS Office clipboard: text/plain + text/html + Files (with image representation)
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/plain', 'text/html', 'Files'],
        items: [
          { kind: 'text', type: 'text/plain', getAsFile: () => null },
          { kind: 'file', type: 'image/png', getAsFile: () => new File(['px'], 'image.png', { type: 'image/png' }) },
        ],
        getData: () => 'Hello from Word',
      },
    })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('does NOT upload files when clipboard has text/html alongside image', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/html', 'Files'],
        items: [
          { kind: 'file', type: 'image/png', getAsFile: () => new File(['px'], 'image.png', { type: 'image/png' }) },
        ],
        getData: () => '<b>rich</b>',
      },
    })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('allows file upload when clipboard has ONLY files (e.g. screenshot paste)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    const file = new File(['px'], 'screenshot.png', { type: 'image/png' })
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['Files'],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
        getData: () => '',
      },
    })
    expect(onUploadFiles).toHaveBeenCalledWith([file])
  })
})

describe('ChatInput optimize: forwards paste content', () => {
  it('sends referenced paste blocks (seq + content) to the optimizer', async () => {
    const token = '[ Paste #1 · 40 lines ]'
    const value = `whats wrong with ${token}`
    const pasteBlocks = [{ id: 'a1', seq: 1, lines: 40, content: 'TRACEBACK: boom' }]

    // URL-aware mock: optimizer endpoint returns the optimize shape; any other
    // app fetch (e.g. SlashCommandMenu's command list) gets a benign empty array
    // so unrelated components don't throw on an unexpected response shape.
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: false, optimized: value }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    // jsdom has no execCommand; the optimizer's onSuccess write-back uses it.
    // Stub it so the post-fetch text write doesn't throw after the assertion.
    ;(document as unknown as { execCommand: () => boolean }).execCommand = vi.fn(() => true)

    renderWithProviders(
      <ChatInput
        value={value}
        onChange={vi.fn()}
        onSend={vi.fn()}
        connected={true}
        pasteBlocks={pasteBlocks}
        onPasteBlocksChange={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

    // The optimize request must carry the full paste content keyed by seq, so
    // the backend can forward it to the model without expanding the token.
    // Find the optimizer call specifically — other app fetches may fire too.
    await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) => typeof c[0] === 'string' && (c[0] as string).includes('/api/optimizer/optimize'),
      )
      expect(call).toBeTruthy()
    })
    const call = fetchMock.mock.calls.find(
      (c) => typeof c[0] === 'string' && (c[0] as string).includes('/api/optimizer/optimize'),
    )!
    const body = JSON.parse((call[1] as RequestInit).body as string)
    expect(body.prompt).toBe(value)
    expect(body.pastes).toEqual([{ seq: 1, content: 'TRACEBACK: boom' }])
  })
})

describe('ChatInput paste: strip trailing blank lines', () => {
  const pasteText = (textarea: HTMLElement, text: string) =>
    fireEvent.paste(textarea, {
      clipboardData: { types: ['text/plain'], items: [], getData: () => text },
    })

  // handlePaste prefers the native document.execCommand('insertText') path so
  // the textarea's own onChange fires. jsdom's execCommand is an unreliable
  // no-op, so by default force it to report failure — that exercises the
  // controlled-value fallback these assertions check. The native path gets its
  // own dedicated test that stubs execCommand to succeed.
  beforeEach(() => {
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  it('trims trailing blank lines a single-line copy carries in (line + empty rows)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'just one line\n\n\n')
    expect(onChange).toHaveBeenCalledWith('just one line')
  })

  it('trims a single trailing newline too', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'hello world\n')
    expect(onChange).toHaveBeenCalledWith('hello world')
  })

  it('strips only the trailing run, not earlier line breaks', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'line1\nline2\n\n')
    expect(onChange).toHaveBeenCalledWith('line1\nline2')
  })

  it('does NOT intercept a clean paste (no trailing blanks) — leaves it to the browser', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'clean line')
    // No preventDefault path taken → onChange fires via the native input event,
    // which jsdom does not dispatch for fireEvent.paste, so our handler stays out.
    expect(onChange).not.toHaveBeenCalled()
  })

  it('leaves a trailing-spaces-only paste untouched (no newline in the run)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'trailing spaces   ')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('does NOT intercept an all-blank-lines clipboard (leaves it to the browser, never a silent no-op)', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), '\n\n\n')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('handles a large space run without pathological backtracking (linear strip)', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />,
    )
    // 200k spaces + a char — the exact shape that made the old trailing-strip
    // regex backtrack quadratically (~2s). The linear scan stops at the first
    // non-whitespace char from the end, so it must finish near-instantly. The
    // chunk is >200 chars so it collapses into a chip.
    const payload = ' '.repeat(200_000) + 'x'
    const t0 = performance.now()
    pasteText(screen.getByRole('textbox'), payload)
    const elapsed = performance.now() - t0
    expect(onPasteBlocksChange).toHaveBeenCalled()
    expect(elapsed).toBeLessThan(1000)
  })

  it('uses the native execCommand insertText path when available (fires the real onChange, not a direct splice)', () => {
    const onChange = vi.fn()
    const exec = vi.fn(() => true)
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = exec
    renderWithProviders(
      <ChatInput value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={vi.fn()} />,
    )
    pasteText(screen.getByRole('textbox'), 'just one line\n\n\n')
    // Inserted via the native input pipeline with the trimmed text …
    expect(exec).toHaveBeenCalledWith('insertText', false, 'just one line')
    // … so handlePaste does NOT splice onChange itself (the textarea's own
    // onChange handles state + picker detection + user-edit flag).
    expect(onChange).not.toHaveBeenCalled()
  })
})
