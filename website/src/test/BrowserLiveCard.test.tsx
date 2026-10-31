import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import BrowserLiveCard, { browserLiveAnchor, isBrowserToolMessage } from '../pages/chat/BrowserLiveCard'
import TurnBlock from '../pages/chat/TurnBlock'
import ChatFooter from '../pages/chat/ChatFooter'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import { api } from '../api/client'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * BrowserLiveCard, driven through the store exactly as the websocket drives it.
 *
 * The card is one element through every state; the tests assert the face each
 * state shows, the writes each control posts, the hand-back key, and that
 * TurnBlock mounts the card once per turn after the turn's last browser row
 * (and not at all for a host that cannot name the session).
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://jspaint.app/', title: 'untitled - Paint', browser: 'user_chrome', headless: false, steps: [{ label: 'open jspaint', status: 'done' }, { label: 'draw roof', status: 'active' }, { label: 'screenshot', status: 'pending' }], step: { index: 1, label: 'click · polygon tool' }, ...over })
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: seq,
})

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Required<Writes>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

/** The observer the card uses to report visibility; tests fire it by hand. */
let observed: { cb: IntersectionObserverCallback; el: Element }[] = []
class FakeIO {
  constructor(private cb: IntersectionObserverCallback) {}
  observe(el: Element) { observed.push({ cb: this.cb, el }) }
  disconnect() { observed = observed.filter(o => o.cb !== this.cb) }
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = []
}
function intersect(visible: boolean) {
  for (const o of observed) o.cb([{ isIntersecting: visible, target: o.el } as IntersectionObserverEntry], o as unknown as IntersectionObserver)
}

beforeEach(() => {
  browserLiveStore.__resetForTests()
  writes = {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => ({ ok: true })),
    release: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true, sent: 1 })),
    open: vi.fn(async () => ({ ok: true })),
    reopen: vi.fn(async () => ({ ok: true })),
    close: vi.fn(async () => ({ ok: true })),
  }
  restore = browserLiveStore.__setWritesForTests(writes)
  observed = []
  vi.stubGlobal('IntersectionObserver', FakeIO)
  // happy-dom has no 2D canvas; the painter is exercised by its own tests.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const card = (running = true) => <BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running={running} />

describe('BrowserLiveCard faces', () => {
  it('renders nothing for a turn the relay has not spoken about', () => {
    const { container } = render(card())
    expect(container.querySelector('[data-testid="browser-live-card"]')).toBeNull()
  })

  it('waiting_allow: names the browser, the one step, and offers the switch', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'waiting_allow' })))
    render(card())
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'waiting_allow')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.waiting_for_allow'))).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.wants_browser', { browser: i18next.t('pages.chat.browserLiveCard.browser_user_chrome') }))).toBeInTheDocument()
    fireEvent.click(screen.getByText(i18next.t('pages.chat.browserLiveCard.switch_to', { browser: 'Chrome' })))
    expect(writes.open).toHaveBeenCalledWith(S)
  })

  it('live: address, browser chip, step strip, current action, Take over', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    // The header row holds Take over and one overflow menu trigger: two controls.
    expect(screen.getByTestId('browser-live-live').querySelector('.border-b')!.querySelectorAll('button')).toHaveLength(2)
    expect(screen.getByLabelText(i18next.t('pages.chat.browserLiveCard.more_actions'))).toBeInTheDocument()
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
    expect(screen.getByText('jspaint.app')).toBeInTheDocument()
    expect(screen.getByText('· untitled - Paint')).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.browser_user_chrome'))).toBeInTheDocument()
    expect(screen.getByText('click · polygon tool')).toBeInTheDocument()
    const steps = screen.getByRole('list', { name: i18next.t('pages.chat.browserLiveCard.steps') })
    expect(steps.querySelectorAll('li')).toHaveLength(3)
    expect(steps.querySelector('[aria-current="step"]')).toHaveTextContent('draw roof')
    // No frame yet: the placeholder, not a canvas.
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.waiting_for_first_frame'))).toBeInTheDocument()
    expect(screen.queryByTestId('browser-live-canvas')).toBeNull()
  })

  it('the first frame swaps the placeholder for the canvas sized to the frame', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas') as HTMLCanvasElement
    expect(canvas).toBeInTheDocument()
    expect(canvas.getAttribute('data-interactive')).toBeNull()
    expect(writes.ack).toHaveBeenCalledWith(S, 1, '')  // the test frame names no stream
  })

  it('Take over claims; the card grows, says who is in control, and Esc hands back', async () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.click(screen.getByTestId('browser-live-take-over'))
    expect(writes.claim).toHaveBeenCalledWith(S)
    const root = screen.getByTestId('browser-live-card')
    expect(root).toHaveAttribute('data-state', 'in_control')
    expect(root).toHaveAttribute('data-grown', 'true')
    const label = screen.getByText(i18next.t('pages.chat.browserLiveCard.you_are_in_control'))
    expect(label).toBeInTheDocument()
    // Narrow-first: the takeover header wraps, and below `sm` only its icons show.
    expect(label.parentElement).toHaveClass('flex-wrap')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.kiro_paused'))).toHaveClass('hidden', 'sm:inline')
    // The step strip and the action bubble leave while the person drives.
    expect(screen.queryByRole('list')).toBeNull()
    const canvas = screen.getByTestId('browser-live-canvas')
    expect(canvas).toHaveAttribute('data-interactive', 'true')
    // Typing goes to the browser, not to the transcript.
    fireEvent.keyDown(canvas, { key: 'a', code: 'KeyA' })
    // Keystrokes go out one at a time on a promise chain, so the send lands a
    // microtask later.
    await waitFor(() => expect(writes.input).toHaveBeenCalledWith(S, expect.objectContaining({ type: 'key', action: 'down', key: 'a', text: 'a' })))
    fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
    // The held "a" is released on the canvas's chain first, then the hand-back goes.
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
    await waitFor(() => expect(writes.release).toHaveBeenCalledWith(S))
    expect(writes.input).toHaveBeenCalledTimes(2)
    expect(writes.input).toHaveBeenLastCalledWith(S, expect.objectContaining({ type: 'key', action: 'up', key: 'a' }))
    expect(writes.input.mock.invocationCallOrder[1]).toBeLessThan(writes.release.mock.invocationCallOrder[0])
  })

  it('the Hand back button releases too', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    render(card())
    fireEvent.click(screen.getByText(i18next.t('pages.chat.browserLiveCard.hand_back')))
    expect(writes.release).toHaveBeenCalledWith(S)
  })

  it('login_needed: the live card with a sign-in banner whose button claims', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'login_needed' })))
    render(card())
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.take_over_to_sign_in'))).toBeInTheDocument()
    const buttons = screen.getAllByText(i18next.t('pages.chat.browserLiveCard.take_over'))
    fireEvent.click(buttons[buttons.length - 1])
    expect(writes.claim).toHaveBeenCalledWith(S)
  })

  it('done: one line with the host, the step count, the browser, Replay and Last frame', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyFrame(frame(2))
      browserLiveStore.applyEvent(ev({ state: 'done' }))
    })
    render(card(false))
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'done')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.browsed_host', { host: 'jspaint.app' }))).toBeInTheDocument()
    const summary = screen.getByText(/tab kept open/)
    expect(summary).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.steps_count', { count: 3 }))
    expect(summary).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.browser_user_chrome'))
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.replay_steps', { count: 3 }))).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.open_in', { browser: 'Chrome' }))).toBeInTheDocument()
    // Two actions in the row, never three (max-two-buttons-per-row).
    expect(screen.getByTestId('browser-live-done-actions').querySelectorAll('button').length).toBe(2)
    // No takeover affordance on a finished turn.
    expect(screen.queryByTestId('browser-live-take-over')).toBeNull()
  })

  it('done on a headless Kiro browser offers no window to open', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'done', browser: 'kiro_chromium', headless: true })))
    render(card(false))
    expect(screen.queryByText(/Open in/)).toBeNull()
    expect(screen.getByText(/headless/)).toBeInTheDocument()
  })

  it('error: the done line carries the server detail through ErrorNotice', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'error', detail: 'Chrome closed the port' })))
    render(card(false))
    expect(screen.getByText('Chrome closed the port')).toBeInTheDocument()
  })

  it('reports visibility from the observer and clears it on unmount', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    const { unmount } = render(card())
    expect(observed).toHaveLength(1)
    act(() => intersect(true))
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    act(() => intersect(false))
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
    act(() => intersect(true))
    unmount()
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, false])
  })

  it('sanitises the page title and the step labels', () => {
    act(() => browserLiveStore.applyEvent(ev({ title: '<img src=x onerror=alert(1)>Paint', step: { index: 0, label: 'click <b>here</b>' } })))
    render(card())
    expect(document.querySelector('img[onerror]')).toBeNull()
    expect(document.querySelector('b')).toBeNull()
  })
})

describe('anchors and the tool-row test', () => {
  it('recognises the browser tool by its trusted name first, then by its pill text', () => {
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '', meta: { tool_name: 'browser' } })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '' })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @browser', cls: '' })).toBe(true)
    // The real wire form: the gateway's managed-MCP tool arrives as @server/tool
    // (regression: the card never mounted for a live turn because this read false).
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @kirocrew-core/browser', cls: '', meta: { mcp_server: 'kirocrew-core' } })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @kirocrew-core/browser', cls: '', meta: { tool_name: 'kirocrew-core___browser' } })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @kirocrew-core/browser_history', cls: '' })).toBe(false)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @kirocrew-core/open_browser', cls: '' })).toBe(false)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser_harness_x', cls: '' })).toBe(false)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '', meta: { tool_name: 'shell' } })).toBe(false)
    expect(isBrowserToolMessage({ role: 'assistant', content: '🔧 browser', cls: '' })).toBe(false)
  })

  it('anchors on the call id, else the timestamp, and reads seconds as ms', () => {
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '', ts: '1761000000.5', meta: { tool_call_id: 'tc1' } })).toEqual({ key: 'call:tc1', ts: 1761000000500 })
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '', ts: '2026-10-22T10:00:00Z' })).toEqual({ key: 'ts:2026-10-22T10:00:00Z', ts: Date.parse('2026-10-22T10:00:00Z') })
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '' })).toEqual({ key: 'ts:', ts: null })
  })
})

describe('TurnBlock mounts the card', () => {
  const row = (id: string, tool: string): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 ${tool}`, cls: '', ts: String(Math.floor(Date.now() / 1000) + Number(id)), meta: { tool_call_id: `tc-${id}`, tool_name: tool } }, idx: Number(id) }) // flake-ok: the settled-turn binding compares the row clock with the record's client clock (±120 s)
  const text: TurnItem = { kind: 'single', msg: { role: 'assistant', content: 'The house is drawn and the screenshot sits in the card above, in red and blue.', cls: '', ts: '1761000099' }, idx: 99 }
  const turn = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
  const renderItem = (it: TurnItem) => <div data-testid={`row-${it.kind === 'single' ? (it.msg.meta?.tool_call_id as string) ?? it.msg.role : 'group'}`} />

  it('once per turn, after the last browser row, when the host names the session', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    const { container } = render(<TurnBlock turn={turn([row('1', 'read'), row('2', 'browser'), row('3', 'browser'), row('4', 'shell')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    const cards = screen.getAllByTestId('browser-live-card')
    expect(cards).toHaveLength(1)
    const order = Array.from(container.querySelectorAll('[data-testid^="row-"], [data-testid="browser-live-card"]')).map(e => e.getAttribute('data-testid'))
    expect(order).toEqual(['row-tc-1', 'row-tc-2', 'row-tc-3', 'browser-live-card', 'row-tc-4'])
  })

  it('not at all without a session key, nor for a turn with no browser call', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(<TurnBlock turn={turn([row('2', 'browser')])} renderItem={renderItem} />)
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
    render(<TurnBlock turn={turn([row('1', 'read')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
  })

  it('a settled turn keeps its card as the done line, outside the fold', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyEvent(ev({ state: 'done' }))
    })
    render(<TurnBlock turn={turn([row('2', 'browser'), text], true)} renderItem={renderItem} browserLiveSessionKey={S} collapseAll />)
    const c = screen.getByTestId('browser-live-card')
    expect(c).toHaveAttribute('data-state', 'done')
    expect(c.closest('[data-collapsed="true"]')).toBeNull()
  })
})

describe('ChatFooter while the person holds the browser', () => {
  it('reads "waiting for you to hand the browser back" instead of Thinking', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    render(<ChatFooter running stopping={false} state="running" lastRole="tool" browserSessionKey={S} />)
    expect(screen.getByRole('status')).toHaveAccessibleName(i18next.t('pages.chat.browserLiveCard.waiting_for_hand_back'))
    act(() => browserLiveStore.applyEvent(ev({ state: 'live' })))
    expect(screen.getByRole('status')).toHaveAccessibleName(i18next.t('pages.chat.chatFooter.thinking'))
  })
})

describe('the finished card reopens live', () => {
  it('done -> Open live (button or thumbnail) -> the same card is live and takes over', async () => {
    act(() => {
      browserLiveStore.applyEvent(ev({ browser: 'kiro_chromium', headless: true }))
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyEvent(ev({ state: 'done', browser: 'kiro_chromium', headless: true, turn_over: true, tab: 'open' }))
    })
    render(card(false))
    const open = screen.getByTestId('browser-live-open-live')
    expect(open).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.open_live'))
    expect(screen.getByTestId('browser-live-done-actions').querySelectorAll('button').length).toBe(2)
    fireEvent.click(open)
    expect(writes.reopen).toHaveBeenCalledWith(S, 't1')
    fireEvent.click(screen.getByTestId('browser-live-done-thumb'))
    expect(writes.reopen).toHaveBeenCalledTimes(2)
    // The server's event turns the same card live.
    act(() => browserLiveStore.applyEvent(ev({ state: 'live', browser: 'kiro_chromium', headless: true, turn_over: true, tab: 'open' })))
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
    fireEvent.click(screen.getByTestId('browser-live-take-over'))
    expect(writes.claim).toHaveBeenCalledWith(S)
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'in_control')
    await act(async () => { await Promise.resolve() })
  })

  it('user Chrome: Open live replaces Open in Chrome in the done row (two actions)', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'open' }))
    })
    render(card(false))
    const row = screen.getByTestId('browser-live-done-actions')
    expect(row.querySelectorAll('button').length).toBe(2)
    expect(screen.getByTestId('browser-live-open-live')).toBeInTheDocument()
    expect(screen.queryByText(i18next.t('pages.chat.browserLiveCard.open_in', { browser: 'Chrome' }))).toBeNull()
  })

  it('the reopened view offers Close (and only a reopened one does)', async () => {
    act(() => browserLiveStore.applyEvent(ev({ turn_over: false })))
    const { unmount } = render(card(true))
    fireEvent.pointerDown(screen.getByLabelText(i18next.t('pages.chat.browserLiveCard.more_actions')), { button: 0, pointerType: 'mouse' })
    expect(screen.queryByTestId('browser-live-close-live')).toBeNull()
    unmount()
    act(() => browserLiveStore.applyEvent(ev({ state: 'live', turn_over: true, tab: 'open' })))
    render(card(true))
    fireEvent.pointerDown(screen.getByLabelText(i18next.t('pages.chat.browserLiveCard.more_actions')), { button: 0, pointerType: 'mouse' })
    const close = await screen.findByTestId('browser-live-close-live')
    fireEvent.click(close)
    expect(writes.close).toHaveBeenCalledWith(S)
  })

  it('a closed tab says so and keeps Replay, with no Open live', () => {
    act(() => {
      browserLiveStore.applyEvent(ev({ browser: 'kiro_chromium', headless: true }))
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyEvent(ev({ state: 'done', browser: 'kiro_chromium', headless: true, turn_over: true, tab: 'closed' }))
    })
    render(card(false))
    expect(screen.queryByTestId('browser-live-open-live')).toBeNull()
    expect(screen.getByText(new RegExp(i18next.t('pages.chat.browserLiveCard.tab_closed')))).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.replay_steps', { count: 3 }))).toBeInTheDocument()
  })

  it('an older card does not offer Open live once a newer turn owns the session', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'open' }))
    })
    render(card(false))
    expect(screen.getByTestId('browser-live-open-live')).toBeInTheDocument()
    act(() => browserLiveStore.applyEvent(ev({ turn_id: 't2', state: 'live', turn_over: false })))
    expect(screen.queryByTestId('browser-live-open-live')).toBeNull()
  })
})

describe('the done card thumbnail without a frame in memory', () => {
  // A card rebuilt from the server's done event (another window, a reconnect)
  // holds no frame: the recording's last frame stands in for the globe.
  const done = () => act(() => browserLiveStore.applyEvent(ev({ state: 'done', browser: 'kiro_chromium', headless: true, turn_over: true, tab: 'closed' })))

  it('shows the recording\'s last frame and keeps Replay', async () => {
    const list = vi.spyOn(api, 'browserLiveRecordings').mockResolvedValue({ turns: [{ turn_id: 't1', started_ts: 1, frame_count: 5 }] })
    done()
    render(card(false))
    act(() => intersect(true))
    const img = await screen.findByTestId('browser-live-done-recorded')
    expect(img.getAttribute('src')).toBe(`/api/browser/live/${encodeURIComponent(S)}/recordings/t1/frames/4.jpg`)
    expect(list).toHaveBeenCalledWith(S, expect.anything())
    expect(screen.getByTestId('browser-live-replay')).toBeInTheDocument()
  })

  it('keeps the globe when the gateway recorded no frame of the turn', async () => {
    vi.spyOn(api, 'browserLiveRecordings').mockResolvedValue({ turns: [{ turn_id: 't1', started_ts: 1, frame_count: 0 }] })
    done()
    render(card(false))
    act(() => intersect(true))
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('browser-live-done-recorded')).toBeNull()
    expect(screen.queryByTestId('browser-live-replay')).toBeNull()
  })
})

describe('a frame that lands after a quick turn ended', () => {
  it('is kept as the thumbnail when the turn had none, and only then', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true }))
      browserLiveStore.applyFrame(frame(1)) // was in flight as the turn ended
    })
    const turn = browserLiveStore.sessionTurns(S).find(t => t.turnId === 't1')!
    expect(turn.hasFrame).toBe(true)
    expect(turn.ring.length).toBe(1)
    act(() => browserLiveStore.applyFrame(frame(2)))
    expect(browserLiveStore.sessionTurns(S).find(t => t.turnId === 't1')!.ring.length).toBe(1)
  })
})

describe('BrowserLiveCard under a policy that turns browsing off', () => {
  const governed = async (browseEnabled: boolean) => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ browse_enabled: browseEnabled } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}>{card()}</QueryClientProvider>)
    return qc
  }

  it('is one line with no takeover, live or replay control', async () => {
    act(() => browserLiveStore.applyEvent(ev()))
    await governed(false)
    const line = await screen.findByTestId('browser-live-policy-off')
    expect(line).toHaveTextContent(i18next.t('pages.settings.browserPanel.policy_off'))
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('watches its own visibility again once the policy is lifted', async () => {
    act(() => browserLiveStore.applyEvent(ev()))
    const qc = await governed(false)
    await screen.findByTestId('browser-live-policy-off')
    act(() => qc.setQueryData(['dashboardConfig'], { browse_enabled: true }))
    const root = await screen.findByTestId('browser-live-card')
    expect(observed.map(o => o.el)).toContain(root)
  })

  it('is the live card as before when the policy permits', async () => {
    act(() => browserLiveStore.applyEvent(ev()))
    await governed(true)
    await waitFor(() => expect(api.dashboardConfig).toHaveBeenCalled())
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
    expect(screen.queryByTestId('browser-live-policy-off')).toBeNull()
  })
})
