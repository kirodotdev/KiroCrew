import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import i18next from 'i18next'
import '../i18n/all'

// The local preview is its own surface with its own tests; here it is a stub that
// says whether it is mounted and active, which is all the mode switch decides.
vi.mock('../components/WebPreviewPanel', () => ({
  default: ({ active }: { active?: boolean }) => <div data-testid="local-preview" data-active={String(active)} />,
}))

import { api } from '../api/client'
import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import PreviewTabBody from '../components/PreviewTabBody'
import BrowserLiveStage from '../pages/chat/BrowserLiveStage'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { browserStageStore } from '../hooks/useBrowserLive.stage'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { TICK_MS } from '../pages/chat/BrowserLiveTimeline'

/**
 * The big view, end to end through its two stores: Expand and Replay on the chat
 * card move the picture to the side panel's Preview tab (or a phone sheet), the
 * card folds to a line with Bring back, the Preview tab switches modes, full
 * screen goes in and out, and the replay timeline plays, seeks, steps, changes
 * speed, returns to Live, draws its overlays at the right place and falls back
 * to the card's own frames when the gateway has no recording.
 */

// The stage is React.lazy in PreviewTabBody and reads its recording through a
// query, so a find crosses a chunk load and a fetch: past Testing Library's 1 s.
const LAZY_STAGE_TIMEOUT_MS = 5000
const S = 'slot:abc'
const T0 = 1_700_000_000_000
const ev = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://github.com/login', title: 'Sign in', browser: 'user_chrome', headless: false, steps: [], ...over })
const frame = (seq: number, at = seq): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, stream: '', seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: at,
})

const RECORDING = {
  turn_id: 't1', started_ts: T0, ended_ts: T0 + 10_000, live: false, width: 640, height: 400,
  frames: [{ i: 0, ts: T0, w: 640, h: 400 }, { i: 1, ts: T0 + 3000, w: 640, h: 400 }, { i: 2, ts: T0 + 6000, w: 640, h: 400 }],
  events: [
    { id: 'a', ts: T0 + 1000, op: 'navigate', label: '', url: 'https://github.com/login' },
    { id: 'b', ts: T0 + 3000, op: 'click', label: 'Click “Sign in”', point: { x: 320, y: 100 } },
    { id: 'c', ts: T0 + 6000, op: 'type', label: 'Type “kiro”', point: { x: 160, y: 300 } },
  ],
}

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>
let writes: Required<Writes>
class FakeIO {
  constructor(private cb: IntersectionObserverCallback) {}
  observe(el: Element) { this.cb([{ isIntersecting: true, target: el } as IntersectionObserverEntry], this as unknown as IntersectionObserver) }
  disconnect() {}
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = []
}

let qc: QueryClient
const wrap = (ui: ReactNode) => <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
const card = (running = true) => <BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running={running} />

function setMobile(mobile: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: mobile && q.includes('max-width'), media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }))
}

beforeEach(() => {
  browserLiveStore.__resetForTests()
  browserStageStore.__resetForTests()
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
  vi.stubGlobal('IntersectionObserver', FakeIO)
  setMobile(false)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  vi.spyOn(api, 'browserLiveRecordings').mockResolvedValue({ turns: [] })
  vi.spyOn(api, 'browserLiveRecording').mockResolvedValue(RECORDING)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  // Unmount before the store resets: their notify() would otherwise re-render the
  // still-mounted card and stage outside act() (this hook runs before RTL's own cleanup).
  cleanup()
  vi.useRealTimers()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  browserStageStore.__resetForTests()
  qc.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function openMenuAndExpand() {
  fireEvent.pointerDown(screen.getByLabelText(i18next.t('pages.chat.browserLiveCard.more_actions')), { button: 0, pointerType: 'mouse' })
  fireEvent.click(screen.getByTestId('browser-live-expand'))
}

describe('Expand moves the picture to the side panel', () => {
  it('opens the panel through the host, folds the card to a line, and Bring back restores it', async () => {
    const host = vi.fn()
    const off = browserStageStore.registerHost(host)
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    render(wrap(<>{card()}<PreviewTabBody sessionKey={S} active /></>))
    expect(screen.getByTestId('preview-tab-body')).toHaveAttribute('data-mode', 'local')
    openMenuAndExpand()
    expect(host).toHaveBeenCalledWith(S)
    expect(browserStageStore.get(S)).toMatchObject({ turnId: null, mode: 'live', surface: 'panel' })
    // The card is the same element, now one line.
    expect(screen.getByTestId('browser-live-card')).toContainElement(screen.getByTestId('browser-live-staged'))
    expect(screen.getByTestId('browser-live-staged')).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.showing_in_side_panel'))
    // The Preview tab is in Agent browser mode, the local preview still mounted but inactive.
    expect(screen.getByTestId('preview-tab-body')).toHaveAttribute('data-mode', 'agent')
    expect(screen.getByTestId('local-preview')).toHaveAttribute('data-active', 'false')
    expect(await screen.findByTestId('browser-live-stage', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toHaveAttribute('data-mode', 'live')
    // Take over works in the big view just like the card.
    fireEvent.click(screen.getByTestId('browser-live-stage-take-over'))
    expect(writes.claim).toHaveBeenCalledWith(S)
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByTestId('browser-live-stage-hand-back'))
    expect(writes.release).toHaveBeenCalledWith(S)
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByTestId('browser-live-bring-back'))
    expect(browserStageStore.get(S)).toBeNull()
    expect(screen.getByTestId('browser-live-live')).toBeInTheDocument()
    expect(screen.getByTestId('preview-tab-body')).toHaveAttribute('data-mode', 'local')
    expect(screen.getByTestId('local-preview')).toHaveAttribute('data-active', 'true')
    off()
  })

  it('Local preview is Bring back; leaving the tab brings the picture back too', async () => {
    browserStageStore.registerHost(() => {})
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    const { rerender } = render(wrap(<PreviewTabBody sessionKey={S} active />))
    // The switch shows once the session has browsed.
    fireEvent.click(screen.getByTestId('preview-mode-agent'))
    expect(browserStageStore.get(S)?.surface).toBe('panel')
    fireEvent.click(screen.getByTestId('preview-mode-local'))
    expect(browserStageStore.get(S)).toBeNull()
    // Bring back sits on the mode row, only while the agent's browser shows.
    expect(screen.queryByTestId('browser-live-stage-bring-back')).toBeNull()
    fireEvent.click(screen.getByTestId('preview-mode-agent'))
    fireEvent.click(screen.getByRole('button', { name: i18next.t('pages.chat.browserLiveStage.bring_back') }))
    expect(browserStageStore.get(S)).toBeNull()
    fireEvent.click(screen.getByTestId('preview-mode-agent'))
    rerender(wrap(<PreviewTabBody sessionKey={S} active={false} />))
    expect(browserStageStore.get(S)).toBeNull()
  })

  it('a session that never browsed shows no mode switch', () => {
    render(wrap(<PreviewTabBody sessionKey={S} active />))
    expect(screen.queryByTestId('preview-mode-agent')).toBeNull()
    expect(screen.getByTestId('local-preview')).toBeInTheDocument()
  })

  it('on a phone Expand opens the full-screen sheet instead', async () => {
    setMobile(true)
    browserStageStore.registerHost(() => {})
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    render(wrap(card()))
    openMenuAndExpand()
    expect(browserStageStore.get(S)?.surface).toBe('sheet')
    const sheet = await screen.findByTestId('browser-live-sheet', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
    expect(sheet).toContainElement(await screen.findByTestId('browser-live-stage', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS }))
    expect(screen.getByTestId('browser-live-staged')).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.showing_full_screen'))
    fireEvent.keyDown(sheet, { key: 'Escape' })
    await waitFor(() => expect(browserStageStore.get(S)).toBeNull())
  })

  it('a new turn under a held browser starts the big view\'s hand-back note empty', async () => {
    browserStageStore.registerHost(() => {})
    act(() => { browserLiveStore.applyEvent(ev({ state: 'in_control' })); browserLiveStore.applyFrame(frame(1)) })
    render(wrap(<PreviewTabBody sessionKey={S} active />))
    act(() => { browserStageStore.open(S, { turnId: null, mode: 'live' }) })
    const note = await screen.findByTestId('browser-live-handback-note', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
    fireEvent.change(note, { target: { value: 'for the old turn' } })
    act(() => { browserLiveStore.applyEvent(ev({ turn_id: 't2', state: 'in_control' })) })
    expect(screen.getByTestId('browser-live-handback-note')).toHaveValue('')
  })

  it('picking a turn that has no card here keeps the sheet open', async () => {
    setMobile(true)
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    render(wrap(card()))
    openMenuAndExpand()
    await screen.findByTestId('browser-live-sheet', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
    // The turn picker's own write: a server-listed older turn no card holds.
    act(() => { browserStageStore.update(S, { turnId: 'older', mode: 'replay' }) })
    expect(screen.getByTestId('browser-live-sheet')).toBeInTheDocument()
  })

  it('with no host (an embed) Expand also falls back to the sheet', async () => {
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    render(wrap(card()))
    openMenuAndExpand()
    expect(browserStageStore.get(S)?.surface).toBe('sheet')
    expect(await screen.findByTestId('browser-live-sheet', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toBeInTheDocument()
  })
})

describe('full screen', () => {
  it('enters and leaves through the Fullscreen API', async () => {
    let fsEl: Element | null = null
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fsEl })
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, get: () => true })
    const request = vi.fn(async () => { fsEl = screen.getByTestId('browser-live-stage'); document.dispatchEvent(new Event('fullscreenchange')) })
    const exit = vi.fn(async () => { fsEl = null; document.dispatchEvent(new Event('fullscreenchange')) })
    HTMLElement.prototype.requestFullscreen = request as unknown as HTMLElement['requestFullscreen']
    document.exitFullscreen = exit as unknown as Document['exitFullscreen']
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    const stage = browserStageStore.open(S, { turnId: null, mode: 'live' })
    render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    const btn = await screen.findByTestId('browser-live-stage-fullscreen', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
    expect(btn).toHaveAttribute('aria-label', i18next.t('pages.chat.browserLiveStage.full_screen'))
    await act(async () => { fireEvent.click(btn) })
    expect(request).toHaveBeenCalled()
    expect(screen.getByTestId('browser-live-stage')).toHaveAttribute('data-fullscreen', 'true')
    expect(btn).toHaveAttribute('aria-label', i18next.t('pages.chat.browserLiveStage.exit_full_screen'))
    // A narrow screen keeps the steps under the picture; only md and up put them beside it.
    const body = screen.getByTestId('browser-live-stage-body')
    expect(body).toHaveClass('flex-col', 'md:flex-row')
    expect(body).not.toHaveClass('flex-row')
    // Bring back hides in full screen (Esc and Exit full screen are the ways out).
    expect(screen.queryByTestId('browser-live-stage-bring-back')).toBeNull()
    await act(async () => { fireEvent.click(btn) })
    expect(exit).toHaveBeenCalled()
    expect(screen.getByTestId('browser-live-stage')).not.toHaveAttribute('data-fullscreen')
    delete (document as unknown as Record<string, unknown>).fullscreenElement
    delete (document as unknown as Record<string, unknown>).fullscreenEnabled
  })
})

describe('the replay timeline', () => {
  async function doneCardReplay() {
    browserStageStore.registerHost(() => {})
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'closed' }))
    })
    render(wrap(<>{card(false)}<PreviewTabBody sessionKey={S} active /></>))
    fireEvent.click(screen.getByTestId('browser-live-replay'))
    await screen.findAllByTestId('browser-replay-marker', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
  }
  const clock = () => screen.getByTestId('browser-replay-clock').textContent

  it('Replay on a finished card opens the big view at the start and plays', async () => {
    await doneCardReplay()
    expect(browserStageStore.get(S)).toMatchObject({ turnId: 't1', mode: 'replay', surface: 'panel' })
    expect(screen.getByTestId('browser-live-stage')).toHaveAttribute('data-mode', 'replay')
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:00', total: '0:10' }))
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getAllByTestId('browser-replay-marker').map(m => m.getAttribute('data-op'))).toEqual(['navigate', 'click', 'type'])
    expect(screen.getByTestId('browser-live-staged')).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.replaying'))
  })

  it('a marker or a step seeks and pauses; the overlay lands at the action’s point', async () => {
    await doneCardReplay()
    fireEvent.click(screen.getAllByTestId('browser-replay-marker')[1])
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:03', total: '0:10' }))
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-pressed', 'false')
    // 320,100 in a 640x400 recording is 50% across, 25% down.
    const point = screen.getByTestId('browser-replay-overlay-point')
    expect(point).toHaveAttribute('data-left', '50.00')
    expect(point).toHaveAttribute('data-top', '25.00')
    expect(screen.getByTestId('browser-replay-overlay')).toHaveAttribute('data-op', 'click')
    expect(screen.getByTestId('browser-replay-canvas')).toHaveAttribute('data-frame-index', '1')
    // The step list jumps too, and marks where the head is.
    fireEvent.click(screen.getAllByTestId('browser-replay-step')[2])
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:06', total: '0:10' }))
    expect(screen.getAllByTestId('browser-replay-step')[2]).toHaveAttribute('aria-current', 'step')
    expect(screen.getByTestId('browser-replay-overlay-label')).toHaveTextContent('Type “kiro”')
  })

  it('Next and Previous step through the actions; the keyboard does the same', async () => {
    await doneCardReplay()
    fireEvent.click(screen.getByTestId('browser-replay-play'))  // pause at 0
    fireEvent.click(screen.getByTestId('browser-replay-next'))
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:01', total: '0:10' }))
    fireEvent.click(screen.getByTestId('browser-replay-next'))
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:03', total: '0:10' }))
    fireEvent.click(screen.getByTestId('browser-replay-prev'))
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:01', total: '0:10' }))
    const view = screen.getByTestId('browser-live-stage')
    fireEvent.keyDown(view, { key: 'ArrowRight' })
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:03', total: '0:10' }))
    fireEvent.keyDown(view, { key: 'End' })
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:10', total: '0:10' }))
    fireEvent.keyDown(view, { key: ' ' })  // play at the end starts over
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-pressed', 'true')
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:00', total: '0:10' }))
  })

  it('keys pressed inside the turn menu leave the player alone; the picture is a labelled image', async () => {
    await doneCardReplay()
    fireEvent.click(screen.getByTestId('browser-replay-play'))  // pause at 0
    const view = screen.getByTestId('browser-live-stage')
    const canvas = screen.getByTestId('browser-replay-canvas')
    expect(canvas).toHaveAttribute('aria-hidden', 'true')
    expect(canvas.parentElement).toHaveAttribute('role', 'img')
    const playing = screen.getByTestId('browser-replay-play').getAttribute('aria-pressed')
    const menu = document.createElement('div')
    menu.setAttribute('role', 'menu')
    const item = document.createElement('div')
    item.setAttribute('role', 'menuitem')
    menu.appendChild(item)
    view.appendChild(menu)
    fireEvent.keyDown(item, { key: ' ' })
    fireEvent.keyDown(item, { key: 'End' })
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-pressed', playing ?? 'false')
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:00', total: '0:10' }))
    menu.remove()
  })

  it('speed changes how fast the head moves', async () => {
    await doneCardReplay()
    fireEvent.click(screen.getByTestId('browser-replay-play'))  // pause
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    fireEvent.click(screen.getByTestId('browser-replay-speed-4'))
    expect(screen.getByTestId('browser-replay-speed-4')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByTestId('browser-replay-play'))
    act(() => { vi.advanceTimersByTime(TICK_MS * 10) })  // 500 ms of wall time at 4x
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:02', total: '0:10' }))
    fireEvent.click(screen.getByTestId('browser-replay-speed-1'))
    act(() => { vi.advanceTimersByTime(TICK_MS * 20) })  // +1 s at 1x
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:03', total: '0:10' }))
    act(() => { vi.advanceTimersByTime(20_000) })        // runs out: stops at the end
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByTestId('browser-replay-play')).toHaveAttribute('aria-label', i18next.t('pages.chat.browserLiveStage.replay_from_start'))
  })

  it('while the turn is running, scrubbing leaves Live and the Live button comes back', async () => {
    vi.mocked(api.browserLiveRecording).mockResolvedValue({ ...RECORDING, live: true, ended_ts: null })
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    const stage = browserStageStore.open(S, { turnId: null, mode: 'live' })
    const { rerender } = render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    await screen.findAllByTestId('browser-replay-marker', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })
    expect(screen.getByTestId('browser-live-stage')).toHaveAttribute('data-mode', 'live')
    expect(screen.queryByTestId('browser-replay-go-live')).toBeNull()
    fireEvent.click(screen.getAllByTestId('browser-replay-marker')[0])
    const s2 = browserStageStore.get(S)!
    expect(s2).toMatchObject({ mode: 'replay', turnId: 't1' })
    rerender(wrap(<BrowserLiveStage sessionKey={S} stage={s2} active />))
    expect(screen.getByTestId('browser-live-stage')).toHaveAttribute('data-mode', 'replay')
    expect(clock()).toBe(i18next.t('pages.chat.browserLiveStage.time_of', { at: '0:01', total: '0:06' }))
    fireEvent.click(screen.getByTestId('browser-replay-go-live'))
    const s3 = browserStageStore.get(S)!
    expect(s3.mode).toBe('live')
    rerender(wrap(<BrowserLiveStage sessionKey={S} stage={s3} active />))
    expect(screen.getByTestId('browser-live-stage')).toHaveAttribute('data-mode', 'live')
  })

  it('a gateway with no recordings (404) replays the card’s own frames', async () => {
    vi.mocked(api.browserLiveRecording).mockResolvedValue(null)
    vi.mocked(api.browserLiveRecordings).mockResolvedValue(null)
    act(() => {
      browserLiveStore.applyEvent(ev(), T0)
      browserLiveStore.applyFrame(frame(1, T0 + 100))
      browserLiveStore.applyEvent(ev({ step: { index: 0, label: 'click Sign in' } }), T0 + 2000)
      browserLiveStore.applyFrame(frame(2, T0 + 2100))
      browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'closed' }), T0 + 4000)
    })
    const stage = browserStageStore.open(S, { turnId: 't1', mode: 'replay' })
    render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    await waitFor(() => expect(api.browserLiveRecording).toHaveBeenCalled())
    expect(await screen.findByTestId('browser-replay-canvas', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getAllByTestId('browser-replay-marker').map(m => m.getAttribute('data-op'))).toEqual(['navigate', 'other'])
    expect(screen.getAllByTestId('browser-replay-step')[1]).toHaveTextContent('click Sign in')
  })
})

describe('replay request errors', () => {
  it('shows both query errors while retaining fallback playback', async () => {
    vi.mocked(api.browserLiveRecording).mockRejectedValue(new Error('Recording index offline'))
    vi.mocked(api.browserLiveRecordings).mockRejectedValue(new Error('Recording list offline'))
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)); browserLiveStore.applyEvent(ev({ state: 'done' })) })
    const stage = browserStageStore.open(S, { turnId: 't1', mode: 'replay' })
    render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    expect(await screen.findByTestId('browser-live-stage-recording-error', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toHaveTextContent('Recording index offline')
    expect(await screen.findByTestId('browser-live-stage-recordings-error', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toHaveTextContent('Recording list offline')
    expect(await screen.findByTestId('browser-replay-canvas', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toBeInTheDocument()
  })

  it('shows failures even with no local turn or recording', async () => {
    vi.mocked(api.browserLiveRecording).mockRejectedValue(new Error('Recording index offline'))
    vi.mocked(api.browserLiveRecordings).mockRejectedValue(new Error('Recording list offline'))
    const stage = browserStageStore.open(S, { turnId: 't1', mode: 'replay' })
    render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    expect(await screen.findByTestId('browser-live-stage-recording-error', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toHaveTextContent('Recording index offline')
    expect(await screen.findByTestId('browser-live-stage-recordings-error', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toHaveTextContent('Recording list offline')
  })

  it('treats the intentional missing-recording answer as a quiet fallback', async () => {
    vi.mocked(api.browserLiveRecording).mockResolvedValue(null)
    vi.mocked(api.browserLiveRecordings).mockResolvedValue(null)
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)); browserLiveStore.applyEvent(ev({ state: 'done' })) })
    const stage = browserStageStore.open(S, { turnId: 't1', mode: 'replay' })
    render(wrap(<BrowserLiveStage sessionKey={S} stage={stage} active />))
    expect(await screen.findByTestId('browser-replay-canvas', undefined, { timeout: LAZY_STAGE_TIMEOUT_MS })).toBeInTheDocument()
    await waitFor(() => expect(qc.isFetching()).toBe(0))
    expect(screen.queryByTestId('browser-live-stage-recording-error')).toBeNull()
    expect(screen.queryByTestId('browser-live-stage-recordings-error')).toBeNull()
  })
})
