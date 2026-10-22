/**
 * ChatEmbed collapses a long paste the way the main chat does, through the
 * same chat-core Paste atom (`chat-core/composer/pasteEditor.ts` for the
 * editor, `composerPastes.ts` for the blocks):
 *
 * - a large paste becomes a `[ Paste #N · M lines ]` token in the draft, not
 *   its full text, and the chip mirror draws behind it;
 * - the send expands the token on the wire and carries the block on
 *   `meta.pastes`, the main chat's shape;
 * - a refused send hands the token back WITH its block, so the retry still
 *   sends the full text;
 * - the token deletes as one unit;
 * - with "Show pasted text in full" on, nothing collapses.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()
const mockPost = vi.fn()
/** The rows the transcript was last handed. */
const { shown } = vi.hoisted(() => ({ shown: { rows: [] as Array<{ role: string; content: string; meta?: Record<string, unknown> }> } }))

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList({ messages }: { messages: typeof shown.rows }, ref: React.Ref<unknown>) {
    shown.rows = messages
    React.useImperativeHandle(ref, () => ({ scrollToBottom: () => {} }), [])
    return <div data-testid="chat-message-list" />
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

// Over both collapse thresholds (4 lines, >200 chars).
const BIG = `${'x'.repeat(250)}\nsecond\nthird\nfourth`
const TOKEN = /\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 4 lines \]/

let queryClient: QueryClient
const editor = () => screen.getByLabelText('Chat message') as HTMLTextAreaElement

async function mount(props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  await act(async () => {
    render(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="slot-1" {...props} /></QueryClientProvider>)
  })
}

async function paste(text: string) {
  await act(async () => {
    fireEvent.paste(editor(), { clipboardData: { types: ['text/plain'], items: [], files: [], getData: () => text } })
  })
}

async function send() {
  await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
}

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  localStorage.clear()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({ ok: true, dispatched: true })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatEmbed long-paste collapse', () => {
  it('a large paste becomes a chip token, drawn by the mirror behind the textarea', async () => {
    await mount()
    await paste(BIG)
    expect(editor().value).toMatch(new RegExp(`^${TOKEN.source}$`))
    expect(editor().value).not.toContain('second')
    expect(document.querySelector('[data-paste-seq="1"]')).not.toBeNull()
  })

  it('the send expands the token on the wire and carries the block on meta.pastes', async () => {
    await mount()
    await act(async () => { fireEvent.change(editor(), { target: { value: 'look:' } }) })
    editor().setSelectionRange(5, 5)
    await paste(BIG)
    await send()
    expect(mockPost).toHaveBeenCalledTimes(1)
    const body = mockPost.mock.calls[0][1] as { message: string; meta: { pastes?: Array<{ seq: number; content: string }> } }
    expect(body.message).toBe(`look:\n${BIG}`)
    expect(body.meta.pastes).toEqual([expect.objectContaining({ seq: 1, lines: 4, content: BIG })])
    expect(editor().value).toBe('')
    expect(document.querySelector('[data-paste-seq]')).toBeNull()
  })

  it('the optimistic bubble carries the block too, so it draws the chip until the server row lands', async () => {
    mockPost.mockReturnValue(new Promise(() => {}))
    await mount()
    await paste(BIG)
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
    const own = shown.rows.filter(r => r.role === 'user')
    expect(own).toHaveLength(1)
    expect(own[0].content).toBe(BIG)
    expect(own[0].meta?.pastes).toEqual([expect.objectContaining({ seq: 1, content: BIG })])
  })

  it('a refused send hands the chip back with its block, so the retry still sends the full text', async () => {
    mockPost.mockRejectedValueOnce(new ApiError(409, 'API 409: {"error":"busy"}', '{"error":"busy"}'))
    await mount()
    await paste(BIG)
    await send()
    expect(editor().value).toMatch(new RegExp(`^${TOKEN.source}$`))
    expect(document.querySelector('[data-paste-seq="1"]')).not.toBeNull()
    await send()
    expect(mockPost).toHaveBeenCalledTimes(2)
    expect((mockPost.mock.calls[1][1] as { message: string }).message).toBe(BIG)
  })

  it('Backspace after the chip deletes the whole token and its block', async () => {
    await mount()
    await paste(BIG)
    const ta = editor()
    ta.setSelectionRange(ta.value.length, ta.value.length)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })
    expect(editor().value).toBe('')
    expect(document.querySelector('[data-paste-seq]')).toBeNull()
  })

  it('undo after deleting a selected chip brings its block back, so the send still carries the full text', async () => {
    await mount()
    await paste(BIG)
    const ta = editor()
    const token = ta.value
    // A selected chip falls through to the browser's own deletion.
    ta.setSelectionRange(0, token.length)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })
    await act(async () => { fireEvent.change(ta, { target: { value: '' } }) })
    expect(document.querySelector('[data-paste-seq]')).toBeNull()
    await act(async () => { fireEvent.keyDown(editor(), { key: 'z', ctrlKey: true }) })
    expect(editor().value).toBe(token)
    expect(document.querySelector('[data-paste-seq="1"]')).not.toBeNull()
    await send()
    expect((mockPost.mock.calls[0][1] as { message: string }).message).toBe(BIG)
  })

  it('the first edit after a slot switch is undoable: Ctrl+Z after deleting a chip brings it back', async () => {
    let view!: ReturnType<typeof render>
    await act(async () => {
      view = render(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="slot-1" /></QueryClientProvider>)
    })
    await paste(BIG)
    const token = editor().value
    await act(async () => {
      view.rerender(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="slot-2" /></QueryClientProvider>)
    })
    // The embed keeps the draft across the switch, so the history is still
    // settling when the user's first edit lands.
    expect(editor().value).toBe(token)
    const ta = editor()
    ta.setSelectionRange(ta.value.length, ta.value.length)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })
    expect(editor().value).toBe('')
    await act(async () => { fireEvent.keyDown(editor(), { key: 'z', ctrlKey: true }) })
    expect(editor().value).toBe(token)
    expect(document.querySelector('[data-paste-seq="1"]')).not.toBeNull()
  })

  it('"Show pasted text in full" keeps a large paste as text', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showFullPastes: true }))
    await mount()
    // Trailing blank lines make the paste take its own insert path, so the
    // full text is observable here rather than being left to the browser.
    await paste(`${BIG}\n\n`)
    expect(editor().value).toBe(BIG)
    expect(document.querySelector('[data-paste-seq]')).toBeNull()
  })

  it("a host onSend gets the blocks, and its stored row retires the optimistic one though the host stripped the text", async () => {
    // A paste ending in whitespace: a host endpoint that strips what it
    // stores must still be recognised as this send, or the message shows twice.
    const TRAILING = `${BIG}   `
    const onSend = vi.fn(() => Promise.resolve())
    await mount({ onSend })
    await paste(TRAILING)
    await send()
    expect(onSend).toHaveBeenCalledWith(TRAILING, { pastes: [expect.objectContaining({ seq: 1, content: TRAILING })], sendId: expect.any(String) })
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: TRAILING.trim(), cls: 'user', ts: '2026-01-01T00:00:05Z' }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(shown.rows.filter(r => r.role === 'user')).toHaveLength(1)
  })

  it('turning "Show pasted text in full" on elsewhere applies to the next paste without a remount', async () => {
    await mount()
    localStorage.setItem('mc-chat-config', JSON.stringify({ showFullPastes: true }))
    await act(async () => { window.dispatchEvent(new Event('mc-config-changed')) })
    await paste(`${BIG}\n\n`)
    expect(editor().value).toBe(BIG)
    expect(document.querySelector('[data-paste-seq]')).toBeNull()
  })
})
