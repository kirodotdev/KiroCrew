import { describe, it, expect, vi } from 'vitest'
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
