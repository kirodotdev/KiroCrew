import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import { ReplayStepList, ReplayTimeline, clusterMarkers, useReplayPlayer } from '../pages/chat/BrowserLiveTimeline'
import { foldLoopRuns, loopLabel, loopProgress, stopSentence } from '../pages/chat/BrowserLiveLoop'
import { BrowserLiveHandbackBar, DEFAULT_HANDBACK_CHOICES, handbackChoices } from '../pages/chat/BrowserLiveHandback'
import { browserLiveStore, readLoop, HANDBACK_NOTE_MAX, type BrowserLiveLoop } from '../hooks/useBrowserLive'
import { readRecording, offsetOf } from '../hooks/useBrowserLive.replay'
import { BROWSER_HANDBACK_CHAT_EVENT } from '../api/client/browserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The loop line (a `reflex` loop's counters, its budget bar and why it
 * stopped), the step folding a loop's many steps need, the replay timeline's
 * markers for a dense run, and the hand-back bar: what the person says as they
 * hand the browser back, and that nothing typed there reaches the page.
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({
  session_key: S, turn_id: 't1', state: 'live', url: 'https://play2048.co/', title: '2048',
  browser: 'kiro_chromium', headless: true, steps: [], ...over,
})
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, stream: '', seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: seq,
})
const LOOP = { moves: 37, rate: 6.24, steps: 40, max_steps: 120, seconds: 5.9, max_seconds: 45, stop: null }

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Required<Writes>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>
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

beforeEach(() => {
  browserLiveStore.__resetForTests()
  writes = {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => ({ ok: true })),
    release: vi.fn(async () => ({ ok: true, delivered: 'agent' })),
    input: vi.fn(async () => ({ ok: true, sent: 1 })),
    open: vi.fn(async () => ({ ok: true })),
    reopen: vi.fn(async () => ({ ok: true })),
    close: vi.fn(async () => ({ ok: true })),
  }
  restore = browserLiveStore.__setWritesForTests(writes)
  vi.stubGlobal('IntersectionObserver', FakeIO)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  // Unmount before the store reset: its notify() would otherwise re-render the
  // still-mounted card outside act() (this hook runs before RTL's own cleanup).
  cleanup()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const card = () => <BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running />
const live = (over: Record<string, unknown> = {}) => act(() => {
  browserLiveStore.applyEvent(ev(over))
  browserLiveStore.applyFrame(frame(1))
})

// ---- the loop line --------------------------------------------------------------

describe('the loop line', () => {
  it('reads the wire field loosely: junk counters are null, no field is no loop', () => {
    expect(readLoop(LOOP)).toEqual({ moves: 37, rate: 6.24, steps: 40, maxSteps: 120, seconds: 5.9, maxSeconds: 45, stop: null })
    expect(readLoop({ moves: '3', rate: -1, stop: '' })).toEqual({ moves: null, rate: null, steps: null, maxSteps: null, seconds: null, maxSeconds: null, stop: null })
    expect(readLoop(null)).toBeNull()
  })

  it('shows the counters and a bar for the larger share of its budget', () => {
    live({ loop: LOOP })
    render(card())
    expect(screen.getByTestId('browser-live-loop-line')).toHaveTextContent('Fast mode: 37 moves · 6.2/s')
    const bar = screen.getByTestId('browser-live-loop-progress')
    // 40/120 steps is 33%; 5.9/45 s is 13%: the bar shows the larger.
    expect(bar).toHaveAttribute('aria-valuenow', '33')
    expect(screen.queryByTestId('browser-live-loop-stop')).toBeNull()
  })

  it('has no loop line when the event carries none', () => {
    live({ loop: null })
    render(card())
    expect(screen.queryByTestId('browser-live-loop')).toBeNull()
  })

  it.each([
    ['done', 'stop_done'], ['until', 'stop_until'], ['stuck', 'stop_stuck'], ['risk', 'stop_risk'],
    ['budget', 'stop_budget'], ['user_in_control', 'stop_user_in_control'], ['off_path', 'stop_off_path'],
    ['decision_unavailable', 'stop_decision_unavailable'], ['something_new', 'stop_other'],
  ])('a loop stopped by %s says why, in one sentence, with no bar', (stop, key) => {
    live({ loop: { ...LOOP, stop } })
    render(card())
    expect(screen.getByTestId('browser-live-loop')).toHaveAttribute('data-stop', stop)
    expect(screen.getByTestId('browser-live-loop-stop')).toHaveTextContent(i18next.t(`pages.chat.browserLiveLoop.${key}`))
    expect(stopSentence(stop)).toBe(i18next.t(`pages.chat.browserLiveLoop.${key}`))
    expect(screen.queryByTestId('browser-live-loop-progress')).toBeNull()
  })

  it('says only what it was told, and leaves the bar out without a budget', () => {
    const bare: BrowserLiveLoop = { moves: null, rate: null, steps: 3, maxSteps: null, seconds: null, maxSeconds: null, stop: null }
    expect(loopLabel(bare)).toBe(i18next.t('pages.chat.browserLiveLoop.fast_mode'))
    expect(loopProgress(bare)).toBeNull()
    expect(loopProgress({ ...bare, steps: 500, maxSteps: 100 })).toBe(1)
    expect(loopLabel({ ...bare, moves: 1 })).toBe('Fast mode: 1 move')
  })
})

// ---- folding ---------------------------------------------------------------------

describe('a run of loop steps folds to its newest few', () => {
  const steps = [
    { label: 'Open play2048.co', status: 'done' },
    ...Array.from({ length: 8 }, (_, i) => ({ label: `Reflex ${i + 1}: Press ArrowLeft`, status: i === 7 ? 'active' : 'done', loop: true })),
  ]

  it('the card strip shows the last three and folds the rest behind one control', () => {
    live({ steps, loop: LOOP })
    render(card())
    const strip = screen.getByRole('list', { name: i18next.t('pages.chat.browserLiveCard.steps') })
    expect(within(strip).getByText('Open play2048.co')).toBeInTheDocument()
    expect(within(strip).queryByText('Reflex 5: Press ArrowLeft')).toBeNull()
    expect(within(strip).getByText('Reflex 6: Press ArrowLeft')).toBeInTheDocument()
    const folded = screen.getByTestId('browser-live-folded')
    expect(folded).toHaveTextContent('5 earlier steps folded')
    fireEvent.click(folded)
    expect(within(strip).getByText('Reflex 1: Press ArrowLeft')).toBeInTheDocument()
    expect(screen.queryByTestId('browser-live-folded')).toBeNull()
  })

  it('folds only when it hides two or more, and never the current one', () => {
    const items = [false, true, true, true, true, false, true, true, true, true, true, true]
    const out = foldLoopRuns(items, x => x, new Set())
    // The first run of 4 hides only one: kept whole. The second (6) hides 3.
    expect(out.filter(e => e.kind === 'folded')).toEqual([{ kind: 'folded', count: 3, run: 6 }])
    expect(foldLoopRuns(items, x => x, new Set(), 7).some(e => e.kind === 'folded')).toBe(false)
    expect(foldLoopRuns(items, x => x, new Set([6])).some(e => e.kind === 'folded')).toBe(false)
  })
})

// ---- the replay timeline -----------------------------------------------------------

const T0 = 1_700_000_000_000
const REFLEX = readRecording({
  turn_id: 't1', started_ts: T0, ended_ts: T0 + 20_000, width: 640, height: 400,
  frames: [{ i: 0, ts: T0, w: 640, h: 400 }],
  events: [
    { id: 'n', ts: T0 + 500, op: 'navigate', url: 'https://play2048.co/' },
    ...Array.from({ length: 12 }, (_, i) => ({ id: `r${i}`, ts: T0 + 10_000 + i * 150, op: 'key', label: `Reflex ${i + 1}: Press ArrowLeft`, loop: true })),
    { id: 'z', ts: T0 + 19_000, op: 'click', label: 'Click e9', point: { x: 1, y: 1 } },
  ],
}, S)!

function Timeline({ onSeek }: { onSeek: (t: number) => void }) {
  const player = useReplayPlayer(REFLEX, 'k', 'start')
  return (
    <>
      <ReplayTimeline rec={REFLEX} player={player} live={false} onSeekStep={t => { onSeek(t); player.seek(t) }} />
      <ReplayStepList rec={REFLEX} current={-1} onSeekStep={onSeek} />
    </>
  )
}

describe('reflex steps on the replay timeline', () => {
  it('reads the loop flag from the recording', () => {
    expect(REFLEX.events.filter(e => e.loop)).toHaveLength(12)
  })

  it('groups markers that would cover each other, each group at its first action', () => {
    // 400 px track: 0, then 12 within 90 px of each other at 200..290, then 380.
    const xs = [0, ...Array.from({ length: 12 }, (_, i) => 200 + i * 7.5), 380]
    expect(clusterMarkers(xs, 20).map(g => g.length)).toEqual([1, 3, 3, 3, 3, 1])
    expect(clusterMarkers([0, 5, 10, 30], 20)).toEqual([[0, 1, 2], [3]])
  })

  it('every reflex move is a step, a run of them folds, and a marker seeks to its action exactly', () => {
    const onSeek = vi.fn()
    // A measured track, so the markers group the way the person sees them.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(400)
    render(<Timeline onSeek={onSeek} />)
    const markers = screen.getAllByTestId('browser-replay-marker')
    const keys = markers.filter(m => m.getAttribute('data-op') === 'key')
    expect(keys.length).toBeGreaterThan(0)
    const total = keys.reduce((n, m) => n + Number(m.getAttribute('data-count')), 0)
    expect(total).toBe(12)
    fireEvent.click(keys[0])
    expect(onSeek).toHaveBeenLastCalledWith(offsetOf(REFLEX, 1))
    // The step list: navigate, "9 earlier steps folded", the last 3 moves, the click.
    expect(screen.getByTestId('browser-live-folded')).toHaveTextContent('9 earlier steps folded')
    expect(screen.getAllByTestId('browser-replay-step')).toHaveLength(5)
  })
})

// ---- the hand-back bar -------------------------------------------------------------

describe('the hand-back bar', () => {
  it('offers the card\'s own four choices, or the agent\'s', () => {
    expect(handbackChoices([])).toEqual(DEFAULT_HANDBACK_CHOICES.map(key => i18next.t(key)))
    expect(handbackChoices(['I signed in', 'Not signed in'])).toEqual(['I signed in', 'Not signed in'])
  })

  it('shows on the card while the person holds the browser, with the agent\'s choices', () => {
    live({ state: 'in_control', handback_options: ['I signed in', 'Not signed in', 'Wrong site'] })
    render(card())
    const bar = screen.getByTestId('browser-live-handback')
    expect(within(bar).getAllByTestId('browser-live-handback-choice').map(b => b.textContent)).toEqual(['I signed in', 'Not signed in', 'Wrong site'])
  })

  it('a choice and a note go with the hand-back on Enter', async () => {
    live({ state: 'in_control' })
    render(card())
    fireEvent.click(screen.getByRole('button', { name: "Didn't work" }))
    const note = screen.getByTestId('browser-live-handback-note')
    fireEvent.change(note, { target: { value: '  It says the password is wrong  ' } })
    await act(async () => { fireEvent.submit(note.closest('form')!) })
    expect(writes.release).toHaveBeenCalledWith(S, { choice: "Didn't work", note: 'It says the password is wrong' })
  })

  it('the header\'s Hand back and Esc in the note field say nothing', async () => {
    live({ state: 'in_control' })
    render(card())
    fireEvent.keyDown(screen.getByTestId('browser-live-handback-note'), { key: 'Escape' })
    await act(async () => {})
    expect(writes.release).toHaveBeenCalledWith(S)
  })

  it('nothing typed in the note field reaches the page', async () => {
    live({ state: 'in_control' })
    render(card())
    const note = screen.getByTestId('browser-live-handback-note')
    for (const key of ['a', 'b', 'Enter', 'Tab', 'ArrowLeft']) fireEvent.keyDown(note, { key })
    fireEvent.change(note, { target: { value: 'ab' } })
    await act(async () => {})
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('the note is capped at the gateway\'s limit', async () => {
    const onHandBack = vi.fn()
    render(<BrowserLiveHandbackBar offered={[]} onHandBack={onHandBack} />)
    const note = screen.getByTestId('browser-live-handback-note')
    expect(note).toHaveAttribute('maxLength', String(HANDBACK_NOTE_MAX))
    live({ state: 'in_control' })
    await act(async () => { await browserLiveStore.release(S, { note: 'x'.repeat(HANDBACK_NOTE_MAX + 50) }) })
    expect(writes.release).toHaveBeenLastCalledWith(S, { note: 'x'.repeat(HANDBACK_NOTE_MAX) })
  })

  it('with no agent op to take it, the note becomes the person\'s next chat message', async () => {
    writes.release.mockResolvedValueOnce({ ok: true, delivered: 'chat' })
    const sent = vi.fn()
    window.addEventListener(BROWSER_HANDBACK_CHAT_EVENT, sent)
    live({ state: 'in_control', turn_over: true })
    await act(async () => { await browserLiveStore.release(S, { choice: 'Done', note: 'Signed in as me' }) })
    window.removeEventListener(BROWSER_HANDBACK_CHAT_EVENT, sent)
    expect(sent).toHaveBeenCalledTimes(1)
    expect((sent.mock.calls[0][0] as CustomEvent).detail).toEqual({ slotKey: S, text: 'Done: Signed in as me' })
  })

  it('an agent op that takes the note sends no chat message', async () => {
    const sent = vi.fn()
    window.addEventListener(BROWSER_HANDBACK_CHAT_EVENT, sent)
    live({ state: 'in_control' })
    await act(async () => { await browserLiveStore.release(S, { note: 'done' }) })
    window.removeEventListener(BROWSER_HANDBACK_CHAT_EVENT, sent)
    expect(sent).not.toHaveBeenCalled()
  })
})
