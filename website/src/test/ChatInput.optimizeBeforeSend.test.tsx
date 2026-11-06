import { useEffect, useRef, useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'

/**
 * Settings > Chat > Composer > Optimize Before Sending: the first Send of a draft
 * runs the optimizer and leaves its result in the composer; the next Send
 * sends what the composer holds. Off by default, so Send is unchanged.
 */

const ORIGINAL = 'my prompt'
const OPTIMIZED = 'a much better prompt'

const stubFetch = (ok = true) =>
  vi.fn((url: string) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      return Promise.resolve({ ok, json: async () => ({ changed: true, optimized: OPTIMIZED }) })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

const optimizerCalls = (fetchMock: ReturnType<typeof stubFetch>) =>
  fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/optimizer/optimize')).length

/** A host that owns the draft and clears it on send, like the chat page. */
function Host({ initial, onSend }: { initial: string; onSend: (text: string) => void }) {
  const [value, setValue] = useState(initial)
  return <ChatInput value={value} onChange={setValue} onSend={() => { onSend(value); setValue('') }} connected={true} />
}

/** One composer shared by two chats, like the chat page: the draft of the chat
 *  switched to lands one render after the switch. */
function SlotHost({ slot, drafts, onSend }: { slot: string; drafts: Record<string, string>; onSend: (text: string) => void }) {
  const saved = useRef({ ...drafts })
  const [value, setValue] = useState(drafts[slot])
  useEffect(() => { setValue(saved.current[slot]) }, [slot])
  const change = (v: string) => { saved.current[slot] = v; setValue(v) }
  return (
    <SlotProvider slotId={slot}>
      <ChatInput value={value} onChange={change} onSend={() => { onSend(value); change('') }} connected={true} />
    </SlotProvider>
  )
}

const pressEnter = () => fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
const sendButton = () => screen.getByRole('button', { name: 'Send' })
// The press after an optimize waits for its own gate: the request has settled
// (Send enabled), which lands apart from the rewrite in the composer.
const sendReady = () => waitFor(() => expect(sendButton()).toBeEnabled())

describe('ChatInput: Optimize Before Sending', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.removeItem('mc-chat-config')
  })

  const turnOn = () => saveChatConfig({ ...loadChatConfig(), optimizeBeforeSend: true })

  it('sends the typed text at once while the setting is off (the default)', () => {
    const fetchMock = stubFetch()
    vi.stubGlobal('fetch', fetchMock)
    const onSend = vi.fn()
    renderWithProviders(<Host initial={ORIGINAL} onSend={onSend} />)
    pressEnter()
    expect(onSend).toHaveBeenCalledWith(ORIGINAL)
    expect(optimizerCalls(fetchMock)).toBe(0)
  })

  it('optimizes on the first Send, sends the reviewed text on the next, and optimizes the next draft again', async () => {
    turnOn()
    const fetchMock = stubFetch()
    vi.stubGlobal('fetch', fetchMock)
    const onSend = vi.fn()
    renderWithProviders(<Host initial={ORIGINAL} onSend={onSend} />)

    pressEnter()
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(OPTIMIZED))
    expect(onSend).not.toHaveBeenCalled()
    expect(optimizerCalls(fetchMock)).toBe(1)

    await sendReady()
    fireEvent.click(sendButton())
    expect(onSend).toHaveBeenCalledExactlyOnceWith(OPTIMIZED)
    expect(optimizerCalls(fetchMock)).toBe(1)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'another prompt' } })
    pressEnter()
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(OPTIMIZED))
    expect(optimizerCalls(fetchMock)).toBe(2)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('keeps the typed text and shows the error when the optimizer fails, then sends it on the next Send', async () => {
    turnOn()
    vi.stubGlobal('fetch', stubFetch(false))
    const onSend = vi.fn()
    renderWithProviders(<Host initial={ORIGINAL} onSend={onSend} />)

    pressEnter()
    await screen.findByTestId('optimize-error')
    expect(screen.getByLabelText('Message input')).toHaveValue(ORIGINAL)
    expect(onSend).not.toHaveBeenCalled()

    await sendReady()
    pressEnter()
    expect(onSend).toHaveBeenCalledExactlyOnceWith(ORIGINAL)
  })

  it('keeps a reviewed draft reviewed across a visit to an empty chat', async () => {
    turnOn()
    const fetchMock = stubFetch()
    vi.stubGlobal('fetch', fetchMock)
    const onSend = vi.fn()
    const drafts = { a: ORIGINAL, b: '' }
    const { rerender } = renderWithProviders(<SlotHost slot="a" drafts={drafts} onSend={onSend} />)
    pressEnter()
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(OPTIMIZED))
    await sendReady()

    rerender(<SlotHost slot="b" drafts={drafts} onSend={onSend} />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(''))
    rerender(<SlotHost slot="a" drafts={drafts} onSend={onSend} />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue(OPTIMIZED))

    pressEnter()
    expect(onSend).toHaveBeenCalledExactlyOnceWith(OPTIMIZED)
    expect(optimizerCalls(fetchMock)).toBe(1)
  })

  it('says the optimizer is busy, and sends nothing, while another chat is being optimized', async () => {
    turnOn()
    let settle = () => {}
    const fetchMock = vi.fn((url: string) => url.includes('/api/optimizer/optimize')
      ? new Promise(resolve => { settle = () => resolve({ ok: true, json: async () => ({ changed: false, optimized: ORIGINAL }) }) })
      : Promise.resolve({ ok: true, json: async () => [] }))
    vi.stubGlobal('fetch', fetchMock)
    const onSend = vi.fn()
    const drafts = { a: ORIGINAL, b: 'another prompt' }
    const { rerender } = renderWithProviders(<SlotHost slot="a" drafts={drafts} onSend={onSend} />)
    pressEnter()
    await screen.findByRole('button', { name: 'Cancel optimizing' })

    rerender(<SlotHost slot="b" drafts={drafts} onSend={onSend} />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toHaveValue('another prompt'))
    pressEnter()

    // Waiting is status, not a failure: no alert and no error notice.
    const busy = await screen.findByTestId('optimize-busy')
    expect(busy).toHaveTextContent('Optimizing another chat — please wait')
    expect(busy).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('optimize-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onSend).not.toHaveBeenCalled()
    expect(fetchMock.mock.calls.filter(([url]) => url.includes('/api/optimizer/optimize'))).toHaveLength(1)

    // The notice goes once the other chat's optimize settles.
    settle()
    await waitFor(() => expect(screen.queryByTestId('optimize-busy')).toBeNull())
  })

  it('sends a slash command as typed', () => {
    turnOn()
    const fetchMock = stubFetch()
    vi.stubGlobal('fetch', fetchMock)
    const onSend = vi.fn()
    renderWithProviders(<Host initial="/compact" onSend={onSend} />)
    pressEnter()
    expect(onSend).toHaveBeenCalledWith('/compact')
    expect(optimizerCalls(fetchMock)).toBe(0)
  })
})
