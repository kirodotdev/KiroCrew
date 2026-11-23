/**
 * A sent quote card in the crew window jumps inside the window's OWN transcript
 * (#36545): it is a control, it lands the quoted row in the window's scroller
 * (never a row index an earlier message's markdown forged) and rings it for
 * the main chat's 3 s, and a quote whose row the window has not loaded says it
 * is not among the rows loaded here; that notice dismisses, and clears itself
 * after the main chat's 8 s.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { i18nT } from '../i18n/t'

const mocks = vi.hoisted(() => ({
  crewPeerGet: vi.fn(), crewPeerPost: vi.fn(), listInstances: vi.fn(), instancesCapabilities: vi.fn(), instancesApprovalState: vi.fn(),
}))
// Every other client call (the shared composer's own reads) answers empty.
vi.mock('../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../api/client')>('../api/client')).ApiError,
  api: new Proxy(mocks as Record<string | symbol, unknown>, {
    get: (t, prop) => {
      if (!(prop in t)) t[prop] = vi.fn().mockResolvedValue(prop === 'slashCommands' ? [] : {})
      return t[prop]
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import CrewChatWindow from '../pages/chat/crew-window/CrewChatWindow'
import { openCrewWindow, closeCrewWindow, useCrewWindow, writeCrewDraft } from '../pages/chat/crew-window/crewWindowStore'

class QuietEventSource {
  static CLOSED = 2
  readyState = 0
  close = vi.fn()
  addEventListener() {}
}

function Host() {
  const target = useCrewWindow()
  return target ? <CrewChatWindow target={target} /> : null
}

const QUOTED = 'The deploy key rotates every ninety days.'
const quoteOf = (ts: string, mid: string) => ({ role: 'assistant', text: QUOTED, ts, mid })
let detail: Record<string, unknown>

function renderWindow() {
  const store = createTestStore({
    instances: { warm: { 'cd-1': { local_port: 1, token: 't' } } } as unknown as RootState['instances'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><Provider store={store}><MemoryRouter><Host /></MemoryRouter></Provider></QueryClientProvider>,
  )
}

/** A peer row lands only after two chained queries (capabilities, then the slot detail). */
const PEER_ROW_WAIT = { timeout: 5000 }
const sentCard = async () => screen.findByTestId('quote-card-sent', {}, PEER_ROW_WAIT)

beforeEach(() => {
  vi.stubGlobal('EventSource', QuietEventSource)
  sessionStorage.clear()
  writeCrewDraft({ instanceId: 'cd-1', key: 'k1' }, '')
  detail = {
    running: false,
    messages: [
      { role: 'user', content: 'When does the key rotate?', ts: 't1', meta: { mid: 'm1' } },
      { role: 'assistant', content: QUOTED, ts: 't2', meta: { mid: 'm2' } },
      { role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t2', 'm2') } },
    ],
  }
  mocks.listInstances.mockResolvedValue({ instances: [{ id: 'cd-1', name: 'devbox' }] })
  mocks.crewPeerGet.mockImplementation((_id: string, path: string) =>
    Promise.resolve(path === 'api/chat/slots?crew=all' ? [{ key: 'k1', title: 'Keys', running: false, interrupted: false }] : detail))
  mocks.crewPeerPost.mockResolvedValue({ ok: true })
  mocks.instancesCapabilities.mockResolvedValue({ version_match: true, version: '0.9.0', local_version: '0.9.0' })
  mocks.instancesApprovalState.mockResolvedValue({ yolo: false })
  openCrewWindow({ instanceId: 'cd-1', key: 'k1' })
})

afterEach(() => {
  act(() => { closeCrewWindow() })
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('CrewChatWindow quote card jump', () => {
  it('lands the quoted row in the window and rings it for 3 s', async () => {
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const card = await sentCard()
    expect(card.tagName).toBe('BUTTON')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers() })

    fireEvent.click(card)

    const window = screen.getByTestId('crew-chat-window')
    // The window draws one reply: the quoted one.
    const reply = within(window).getByTestId('crew-window-assistant')
    const replyRow = reply.closest('[data-display-index]')
    expect(replyRow).not.toBeNull()
    expect(scrolled).toEqual([replyRow])
    const ringed = () => within(window).getByTestId('crew-window-assistant').closest('.animate-msg-highlight')
    expect(ringed()).not.toBeNull()
    act(() => { vi.advanceTimersByTime(2999) })
    expect(ringed()).not.toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    expect(ringed()).toBeNull()
    expect(screen.queryByTestId('crew-window-quote-unavailable')).toBeNull()
  })

  it('opens the closed step fold over a quoted working step, then scrolls to that row', async () => {
    // Display rows: 0 the question, 1 a finished turn (the quoted step, a tool
    // call, the answer) whose steps the default Show Thinking Inline folds,
    // 2 the quoting send.
    detail = {
      running: false,
      messages: [
        { role: 'user', content: 'When does the key rotate?', ts: 't1', meta: { mid: 'm1' } },
        { role: 'assistant', content: 'Reading the rotation policy first.', ts: 't2', meta: { mid: 'm2' } },
        { role: 'tool', content: '🔧 Running: cat policy.md', cls: '', ts: 't2a', meta: { tool_call_id: 'c1', tool_input: 'cat policy.md' } },
        { role: 'assistant', content: `${QUOTED} The next rotation is due in March.`, ts: 't2b', meta: { mid: 'm2b' } },
        { role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t2', 'm2') } },
      ],
    }
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const card = await sentCard()
    const window = screen.getByTestId('crew-chat-window')
    const step = () => within(window).getAllByTestId('crew-window-assistant')[0]
    expect(step().closest('[data-fold-row]')).toHaveAttribute('data-collapsed', 'true')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers() })

    fireEvent.click(card)
    expect(step().closest('[data-fold-row]')).not.toHaveAttribute('data-collapsed')
    const ringedRow = step().closest('.animate-msg-highlight')
    expect(ringedRow).not.toBeNull()
    // The window lands the turn; past the fold's transition, the turn scrolls
    // to the quoted row itself.
    act(() => { vi.advanceTimersByTime(250) })
    expect(scrolled).toEqual([step().closest('[data-display-index]'), ringedRow])
  })

  it('lands the real row, not a data-display-index an earlier reply forged in its markdown', async () => {
    // Display rows: 0 a prompt, 1 the forging reply, 2 the question, 3 the
    // quoted reply, 4 the quoting send. Row 1's span comes first in document order.
    detail = {
      running: false,
      messages: [
        { role: 'user', content: 'Where is the rotation note?', ts: 't0', meta: { mid: 'm0' } },
        { role: 'assistant', content: 'See <span data-display-index="3"></span> below.', ts: 't0a', meta: { mid: 'm0a' } },
        { role: 'user', content: 'When does the key rotate?', ts: 't1', meta: { mid: 'm1' } },
        { role: 'assistant', content: QUOTED, ts: 't2', meta: { mid: 'm2' } },
        { role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t2', 'm2') } },
      ],
    }
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const card = await sentCard()

    fireEvent.click(card)

    const window = screen.getByTestId('crew-chat-window')
    const forged = window.querySelector('span[data-display-index="3"]')
    expect(forged).not.toBeNull()
    expect(window.querySelector('[data-display-index="3"]')).toBe(forged)
    const [, reply] = within(window).getAllByTestId('crew-window-assistant')
    const replyRow = reply.closest('[data-display-index]')
    expect(replyRow?.getAttribute('data-display-index')).toBe('3')
    expect(replyRow?.parentElement?.closest('[data-display-index]')).toBeNull()
    expect(scrolled).not.toContain(forged)
    expect(scrolled).toEqual([replyRow])
  })

  it('stops following the bottom when a jump lands, so the notice leaving the dock does not snap it back', async () => {
    detail = {
      running: false,
      messages: [
        { role: 'user', content: 'When does the key rotate?', ts: 't1', meta: { mid: 'm1' } },
        { role: 'assistant', content: QUOTED, ts: 't2', meta: { mid: 'm2' } },
        { role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t2', 'm2') } },
        { role: 'user', content: 'And the old key?', ts: 't4', meta: { mid: 'm4', quote: quoteOf('t-gone', 'm-gone') } },
      ],
    }
    // The dock is measured by a ResizeObserver; record them so the test fires
    // the dock's own measurement once the notice has left it.
    const observers: { cb: () => void; targets: Element[] }[] = []
    vi.stubGlobal('ResizeObserver', class {
      entry: { cb: () => void; targets: Element[] }
      constructor(cb: () => void) { this.entry = { cb, targets: [] }; observers.push(this.entry) }
      observe(el: Element) { this.entry.targets.push(el) }
      unobserve() {}
      disconnect() { this.entry.targets = [] }
    })
    let scroller: HTMLElement | null = null
    const original = Element.prototype.scrollIntoView
    // The smooth scroll that lands the row, centred well above the bottom.
    Element.prototype.scrollIntoView = function () { if (scroller) scroller.scrollTop = 568 }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const cards = await screen.findAllByTestId('quote-card-sent', {}, PEER_ROW_WAIT)
    const dock = screen.getByTestId('crew-window-dock')
    scroller = dock.parentElement!.querySelector<HTMLElement>('[aria-live="polite"]')
    expect(scroller).not.toBeNull()
    let top = 0
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 2000 })
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 400 })
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
    Object.defineProperty(dock, 'offsetHeight', {
      configurable: true,
      get: () => (dock.querySelector('[data-testid="crew-window-quote-unavailable"]') ? 120 : 80),
    })
    const measureDock = () => act(() => { observers.filter(o => o.targets.includes(dock)).forEach(o => o.cb()) })

    // The window opens following the bottom; the unloaded quote's notice grows the dock.
    fireEvent.click(cards[1])
    expect(screen.getByTestId('crew-window-quote-unavailable')).toBeInTheDocument()
    measureDock()
    expect(scroller.scrollTop).toBe(2000)

    // The quoted reply is 40px tall at content offset 680: centred above the
    // 120px dock's 136px inset, it ends at 568.
    const replyRow = within(screen.getByTestId('crew-chat-window')).getByTestId('crew-window-assistant').closest('[data-display-index]')!
    Object.defineProperty(replyRow, 'getBoundingClientRect', { configurable: true, value: () => ({ top: 680 - top, height: 40 }) })
    fireEvent.click(cards[0])
    expect(screen.queryByTestId('crew-window-quote-unavailable')).toBeNull()
    expect(scroller.scrollTop).toBe(568)
    // The dock shrinks as the notice leaves: the follow effect re-runs.
    measureDock()
    expect(scroller.scrollTop).not.toBe(scroller.scrollHeight)
    expect(scroller.scrollTop).toBe(568)
  })

  it('keeps following the bottom when a jump lands there, so the next reply still shows', async () => {
    // The feed this window opens, so the test can say the peer finished a row.
    let chatMessage: ((e: MessageEvent) => void) | undefined
    vi.stubGlobal('EventSource', class {
      static CLOSED = 2
      readyState = 0
      close = vi.fn()
      addEventListener(type: string, fn: (e: MessageEvent) => void) { if (type === 'chat_message') chatMessage = fn }
    })
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    // The row already sits where a centred landing puts it: nothing moves, so no scroll event.
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const card = await sentCard()
    const scroller = screen.getByTestId('crew-window-dock').parentElement!.querySelector<HTMLElement>('[aria-live="polite"]')!
    // The reader is at the bottom of 2000px of rows in a 400px viewport.
    let height = 2000
    let top = 1600
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => height })
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 400 })
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
    // The quoted reply is the latest, 80px tall at content offset 1760: centred, it ends at the bottom.
    const replyRow = within(screen.getByTestId('crew-chat-window')).getByTestId('crew-window-assistant').closest('[data-display-index]')!
    Object.defineProperty(replyRow, 'getBoundingClientRect', { configurable: true, value: () => ({ top: 1760 - top, height: 80 }) })

    fireEvent.click(card)
    expect(scrolled).toEqual([replyRow])

    // The peer finishes a 300px reply; its feed frame's throttled re-read of
    // the slot detail draws it, and the follow writer runs in that commit.
    const NEXT = 'The next rotation is due in March.'
    detail = { ...detail, messages: [...(detail.messages as object[]), { role: 'assistant', content: NEXT, ts: 't4', meta: { mid: 'm4' } }] }
    height = 2300
    chatMessage!({ data: JSON.stringify({ slot: 'k1' }) } as MessageEvent)
    await screen.findByText(NEXT, {}, PEER_ROW_WAIT)
    expect(scroller.scrollTop).toBe(2300)
  })

  it('centres a jump in the strip the dock leaves visible, and follows the bottom by where it lands', async () => {
    const observers: { cb: () => void; targets: Element[] }[] = []
    vi.stubGlobal('ResizeObserver', class {
      entry: { cb: () => void; targets: Element[] }
      constructor(cb: () => void) { this.entry = { cb, targets: [] }; observers.push(this.entry) }
      observe(el: Element) { this.entry.targets.push(el) }
      unobserve() {}
      disconnect() { this.entry.targets = [] }
    })
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    // The row already sits where a centred landing puts it: nothing moves, so no scroll event.
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    renderWindow()
    const card = await sentCard()
    const dock = screen.getByTestId('crew-window-dock')
    const scroller = dock.parentElement!.querySelector<HTMLElement>('[aria-live="polite"]')!
    let height = 2000
    let top = 0
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => height })
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 400 })
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
    Object.defineProperty(dock, 'offsetHeight', { configurable: true, get: () => 80 })
    const resize = (el: Element) => act(() => { observers.filter(o => o.targets.includes(el)).forEach(o => o.cb()) })

    // The 80px dock covers the scroller's bottom 96px, for a scroll into view too.
    resize(dock)
    expect(scroller.style.paddingBottom).toBe('96px')
    expect(scroller.style.scrollPaddingBottom).toBe('96px')

    // The reader is 32px above the bottom (1600), inside the slack. The quoted
    // reply is 40px tall at content offset 1700: centred in the 304px above the
    // dock it ends at 1568, where the reader already is. A full-height centre
    // (1520) would put it 80px up and stop following.
    top = 1568
    const replyRow = within(screen.getByTestId('crew-chat-window')).getByTestId('crew-window-assistant').closest('[data-display-index]')!
    Object.defineProperty(replyRow, 'getBoundingClientRect', { configurable: true, value: () => ({ top: 1700 - top, height: 40 }) })
    fireEvent.click(card)
    expect(scrolled).toEqual([replyRow])

    // An image in the transcript grows it by 100px: the window still follows.
    height = 2100
    resize(scroller.firstElementChild!)
    expect(scroller.scrollTop).toBe(2100)
  })

  it('says it cannot jump when the quoted row is not loaded, and the notice dismisses', async () => {
    detail = {
      running: false,
      messages: [{ role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t-gone', 'm-gone') } }],
    }
    renderWindow()
    fireEvent.click(await sentCard())

    const notice = screen.getByTestId('crew-window-quote-unavailable')
    expect(notice).toHaveTextContent(i18nT('pages.chat.quoteCard.message_not_loaded'))
    expect(screen.getByTestId('crew-window-notices')).toContainElement(notice)
    fireEvent.click(within(notice).getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(screen.queryByTestId('crew-window-quote-unavailable')).toBeNull()
  })

  it('clears the not-loaded notice by itself after 8 s', async () => {
    detail = {
      running: false,
      messages: [{ role: 'user', content: 'Remind the team', ts: 't3', meta: { mid: 'm3', quote: quoteOf('t-gone', 'm-gone') } }],
    }
    renderWindow()
    const card = await sentCard()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers() })

    fireEvent.click(card)
    expect(screen.getByTestId('crew-window-quote-unavailable')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(7999) })
    expect(screen.getByTestId('crew-window-quote-unavailable')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.queryByTestId('crew-window-quote-unavailable')).toBeNull()
  })
})
