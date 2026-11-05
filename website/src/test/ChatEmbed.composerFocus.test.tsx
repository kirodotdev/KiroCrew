/**
 * ChatEmbed's composer keeps keyboard focus across a send.
 *
 * The textarea is `disabled` while a send is in flight, and the Send button
 * is disabled once the draft clears. A browser's focus fixup moves focus to
 * <body> when the focused control becomes disabled, so without a refocus the
 * next keystrokes after Enter or Send went nowhere. happy-dom runs no focus
 * fixup, so `emulateFocusFixup` below does what the browser does.
 *
 * Pinned here:
 * - Enter in the composer: focus is back in the textarea once the send settles.
 * - A click on Send: the same.
 * - Focus the user moved elsewhere during the send stays where they put it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import type { SendReceipt } from '../chat-core/transport/sendTurn'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

const { mockSendTurn } = vi.hoisted(() => ({ mockSendTurn: vi.fn() }))
vi.mock('../chat-core/transport/sendTurn', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/transport/sendTurn')>()
  return { ...actual, sendTurn: mockSendTurn }
})

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList(_props: unknown, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return <ol data-testid="rows" />
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient
let stopFixup: () => void

/** The browser's focus fixup: a focused control that becomes disabled loses
 *  focus to <body>. happy-dom's `blur()` ignores a disabled element, so the
 *  fixup focuses <body> directly. */
function emulateFocusFixup(): () => void {
  const observer = new MutationObserver(records => {
    for (const r of records) {
      const el = r.target as HTMLTextAreaElement
      if (el === document.activeElement && el.disabled) document.body.focus()
    }
  })
  observer.observe(document.body, { attributes: true, attributeFilter: ['disabled'], subtree: true })
  return () => observer.disconnect()
}

/** A send the test settles by hand, so it is in flight for as long as the
 *  test needs. */
function holdSend(): () => Promise<void> {
  let release!: (r: SendReceipt) => void
  mockSendTurn.mockReturnValueOnce(new Promise<SendReceipt>(resolve => { release = resolve }))
  return async () => {
    await act(async () => {
      release({ status: 'dispatched', body: {} })
      await vi.advanceTimersByTimeAsync(50)
    })
  }
}

function renderEmbed() {
  return render(
    <QueryClientProvider client={queryClient}>
      <input aria-label="Elsewhere on the page" />
      <ChatEmbed slotKey="slot-1" />
    </QueryClientProvider>,
  )
}

const composer = () => screen.getByLabelText('Chat message') as HTMLTextAreaElement

async function typeIntoComposer(text: string) {
  await act(async () => { composer().focus() })
  expect(document.activeElement).toBe(composer())
  await act(async () => { fireEvent.change(composer(), { target: { value: text } }) })
}

beforeEach(() => {
  vi.clearAllMocks()
  // `clearAllMocks` keeps a queued `mockReturnValueOnce`. A test that fails
  // before its send consumes the held promise must not pass it to the next one.
  mockSendTurn.mockReset()
  resetEmbedRecoveryForTests()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  stopFixup = emulateFocusFixup()
})

afterEach(() => {
  stopFixup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatEmbed composer focus across a send', () => {
  it('Enter: focus is back in the composer once the send settles', async () => {
    await act(async () => { renderEmbed() })
    const settleSend = holdSend()
    await typeIntoComposer('first message')
    await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter' }) })
    expect(mockSendTurn).toHaveBeenCalledTimes(1)
    // In flight: the disabled textarea lost focus, as it does in a browser.
    expect(composer().disabled).toBe(true)
    expect(document.activeElement).toBe(document.body)

    await settleSend()
    expect(composer().disabled).toBe(false)
    expect(document.activeElement).toBe(composer())
  })

  it('Send button: focus is back in the composer once the send settles', async () => {
    await act(async () => { renderEmbed() })
    const settleSend = holdSend()
    await typeIntoComposer('first message')
    const send = screen.getByLabelText('Send message')
    // A pointer press on a button focuses it before the click lands.
    await act(async () => { send.focus() })
    await act(async () => { fireEvent.click(send) })
    expect(mockSendTurn).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(document.body)

    await settleSend()
    expect(document.activeElement).toBe(composer())
  })

  it('focus the user moved elsewhere during the send stays there', async () => {
    await act(async () => { renderEmbed() })
    const settleSend = holdSend()
    await typeIntoComposer('first message')
    await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter' }) })
    const elsewhere = screen.getByLabelText('Elsewhere on the page')
    await act(async () => { elsewhere.focus() })

    await settleSend()
    expect(composer().disabled).toBe(false)
    expect(document.activeElement).toBe(elsewhere)
  })
})
