/**
 * The embed's editor continues a markdown list on a new line, as the dashboard
 * composer does (`chat-input/listContinuation.ts`). Every new-line path ends in
 * a native `beforeinput` of type insertLineBreak (Shift+Enter, a phone's
 * Return), so these tests drive that event on the embed's own textarea.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

async function mount() {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" />
      </QueryClientProvider>,
    )
  })
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  return screen.getByLabelText('Chat message') as HTMLTextAreaElement
}

/** Type `text` and put a collapsed caret at `caret` (default: the end). */
async function typeAt(ta: HTMLTextAreaElement, text: string, caret = text.length) {
  await act(async () => { fireEvent.change(ta, { target: { value: text } }) })
  ta.setSelectionRange(caret, caret)
}

/** One browser edit of `inputType`. True when the composer made its own edit
 *  and cancelled the browser's. */
function edit(ta: HTMLTextAreaElement, inputType: string) {
  const event = new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType })
  act(() => { ta.dispatchEvent(event) })
  return event.defaultPrevented
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed list continuation', () => {
  it('continues a numbered list with the next number', async () => {
    const ta = await mount()
    await typeAt(ta, '1. first')
    expect(edit(ta, 'insertLineBreak')).toBe(true)
    expect(ta.value).toBe('1. first\n2. ')
    expect(ta.selectionStart).toBe(ta.value.length)
  })

  it('continues a bullet list and a task list', async () => {
    const ta = await mount()
    await typeAt(ta, '- a')
    edit(ta, 'insertLineBreak')
    expect(ta.value).toBe('- a\n- ')
    await typeAt(ta, '- [x] done')
    edit(ta, 'insertParagraph')
    expect(ta.value).toBe('- [x] done\n- [ ] ')
  })

  it('renumbers the items below a new one', async () => {
    const ta = await mount()
    await typeAt(ta, '1. a\n2. b', '1. a'.length)
    edit(ta, 'insertLineBreak')
    expect(ta.value).toBe('1. a\n2. \n3. b')
  })

  it('ends the list on an empty item', async () => {
    const ta = await mount()
    await typeAt(ta, '1. a\n2. ')
    expect(edit(ta, 'insertLineBreak')).toBe(true)
    expect(ta.value).toBe('1. a\n')
  })

  it('Backspace where an item text begins removes the whole marker', async () => {
    const ta = await mount()
    await typeAt(ta, 'note\n- ')
    expect(edit(ta, 'deleteContentBackward')).toBe(true)
    expect(ta.value).toBe('note\n')
  })

  it('leaves a plain line to the browser', async () => {
    const ta = await mount()
    await typeAt(ta, 'just text')
    expect(edit(ta, 'insertLineBreak')).toBe(false)
    expect(ta.value).toBe('just text')
  })

  it('a continued list sends as typed', async () => {
    const ta = await mount()
    await typeAt(ta, '1. first')
    edit(ta, 'insertLineBreak')
    await act(async () => { fireEvent.change(ta, { target: { value: `${ta.value}second` } }) })
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({ message: '1. first\n2. second' }))
  })
})
