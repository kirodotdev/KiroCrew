/**
 * The Context tab runs no timer. It reads its trace once on mount, then re-reads
 * only when a pushed `usage` frame says the trace moved, or when the socket
 * reconnects (`hooks/websocket/contextTraceRefresh.ts`).
 *
 * Driven through the real tab and a real QueryClient with the app's own default
 * staleTime (Infinity), so a test that passed here would also hold in the app.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'

const traceMock = vi.fn()
const capsMock = vi.fn()
const turnMock = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    telemetryContextTrace: (slot: string) => traceMock(slot),
    telemetryContextTurn: (slot: string, turn: number) => turnMock(slot, turn),
    chatSlotSelectionCapabilities: (slot: string) => capsMock(slot),
  },
}))

import { ContextBreakdownTab, type ContextTrace } from '../pages/ContextBreakdownPanel'
import {
  CONTEXT_TRACE_COALESCE_MS as COALESCE_MS,
  contextTraceKey,
  onUsageFrame,
  readSessionProjectionFrame,
  rereadAllContextTraces,
  resetContextTraceRefresh,
} from '../hooks/useWebSocket'

type SessionProjectionFrame = NonNullable<ReturnType<typeof readSessionProjectionFrame>>

const SLOT = 'chat-7'
const EMPTY = 'No context breakdown recorded for this session yet.'

function trace(turns: number): ContextTrace {
  return {
    slot: SLOT,
    turns: Array.from({ length: turns }, (_, i) => ({
      phase: 'turn',
      blocks: { your_message: 10 },
      total_chars: 10,
      ordinal: i + 1,
    })),
    totals: { your_message: 10 * turns },
    injected_chars: 0,
    user_chars: 10 * turns,
    peak_context_used: 0,
    context_window: 0,
    window_days: 14,
    latest:
      turns > 0
        ? {
            turn: turns,
            phase: 'per_turn',
            ours: { message: 10 * turns, memory: 0, rules: 0, skills: 0, other: 0 },
            loop_chars: 0,
            loop_estimated: true,
            engine: null,
            ttft_ms: null,
            context_used: 0,
            context_window: 0,
          }
        : null,
    trend: [],
  }
}

function frame(revision: number, over: Partial<SessionProjectionFrame> = {}): SessionProjectionFrame {
  return { unit: 'unit-1', slot: `dashboard:${SLOT}`, name: 'usage', seq: 1, value: {}, revision, ...over }
}

/** A read the test resolves by hand, to hold one in flight. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

let qc: QueryClient

function mount() {
  return render(createElement(QueryClientProvider, { client: qc }, createElement(ContextBreakdownTab, { slot: SLOT })))
}

/** Let resolved promises and react-query's notify batch run. */
async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

/** Fire the coalesce window, then settle. */
async function window() {
  await act(async () => { await vi.advanceTimersByTimeAsync(COALESCE_MS) })
  await settle()
}

// react-query batches its observer notifications on a setTimeout it captured at
// import, before the fake clock existed; run them as microtasks so a settled read
// reaches the rendered tab inside `settle()`.
notifyManager.setScheduler(queueMicrotask)

beforeEach(() => {
  vi.useFakeTimers()
  traceMock.mockReset()
  capsMock.mockReset()
  capsMock.mockResolvedValue({ known: true, backend: 'kas' })
  turnMock.mockReset()
  resetContextTraceRefresh()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(() => {
  cleanup()
  qc.clear()
  vi.useRealTimers()
})

describe('Context tab: pushed frames replace the poll', () => {
  it('reads once on mount, and a usage frame updates the panel with no poll', async () => {
    traceMock.mockResolvedValueOnce(trace(0)).mockResolvedValueOnce(trace(1))
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText(EMPTY)).toBeTruthy()

    expect(onUsageFrame(qc, frame(5))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(EMPTY)).toBeNull()
  })

  it('leaves no interval running: minutes pass and nothing reads', async () => {
    // react-query's refetchInterval is a setInterval; the tab must arm none.
    const intervals = vi.spyOn(globalThis, 'setInterval')
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
    expect(traceMock).toHaveBeenCalledTimes(1)
    expect(intervals).not.toHaveBeenCalled()
    intervals.mockRestore()
  })

  it('drops a frame at or below the revision already seen for its unit', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(onUsageFrame(qc, frame(5))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    expect(onUsageFrame(qc, frame(5))).toBe('stale')
    expect(onUsageFrame(qc, frame(4))).toBe('stale')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('judges revisions per unit, so a new unit of the slot still re-reads', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    onUsageFrame(qc, frame(9))
    await window()
    expect(onUsageFrame(qc, frame(1, { unit: 'unit-2' }))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(3)
  })

  it('ignores frames for other folds', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(onUsageFrame(qc, frame(5, { name: 'status' }))).toBe('ignored')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(1)
  })

  it('merges a burst of frames into one read', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    for (let r = 1; r <= 6; r++) onUsageFrame(qc, frame(r, { unit: r % 2 ? 'unit-1' : 'unit-2' }))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('a frame during a read owes exactly one more read, and the newer one lands last', async () => {
    traceMock.mockResolvedValueOnce(trace(0))
    mount()
    await settle()

    const first = deferred<ContextTrace>()
    const second = deferred<ContextTrace>()
    traceMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    onUsageFrame(qc, frame(1))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // Three more windows' worth of frames while that read is on the wire.
    onUsageFrame(qc, frame(2))
    await window()
    onUsageFrame(qc, frame(3))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // The old read settles; exactly one follow-up starts.
    first.resolve(trace(1))
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(3)
    second.resolve(trace(3))
    await settle()
    expect(qc.getQueryData<ContextTrace>(contextTraceKey(SLOT))?.turns).toHaveLength(3)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(traceMock).toHaveBeenCalledTimes(3)
  })

  it('re-reads once on a reconnect, and a restarted gateway\'s low revisions are not stale', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    onUsageFrame(qc, frame(40))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // The socket reopens on a restarted gateway: what useWebSocket's onopen does.
    resetContextTraceRefresh()
    rereadAllContextTraces(qc)
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(3)

    // The new process mints from 1 again; its first frame must not be dropped.
    expect(onUsageFrame(qc, frame(1))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(4)
  })

  it('names the harness the selection-capabilities route reports, Kiro CLI included', async () => {
    const unsplit = (): ContextTrace => {
      const t = trace(1)
      return { ...t, latest: { ...t.latest!, engine: null, loop_estimated: true, context_used: 9_000, context_window: 1_000_000 } }
    }
    traceMock.mockResolvedValue(unsplit())
    mount()
    await settle()
    expect(capsMock).toHaveBeenCalledWith(SLOT)
    // Kiro Agent: the turn is not split yet, never "switch to Kiro Agent".
    const kasGlyph = screen.getByRole('button', { name: /more information/i })
    await act(async () => { kasGlyph.focus(); kasGlyph.dispatchEvent(new FocusEvent('focus')) })
    expect(document.body.textContent).toMatch(/Estimated\. The engine hasn't measured this turn yet\./)
    expect(document.body.textContent).not.toMatch(/pick Kiro Agent/)
    cleanup()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    // Kiro CLI's runtime id is the empty string.
    capsMock.mockResolvedValue({ known: true, backend: '' })
    mount()
    await settle()
    const glyph = screen.getByRole('button', { name: /more information/i })
    await act(async () => { glyph.focus(); glyph.dispatchEvent(new FocusEvent('focus')) })
    expect(document.body.textContent).toMatch(/Kiro CLI doesn't report/)
  })

  it('shows a failed harness read and gives no harness advice', async () => {
    const t = trace(1)
    traceMock.mockResolvedValue({ ...t, latest: { ...t.latest!, engine: null, context_used: 9_000, context_window: 1_000_000 } })
    capsMock.mockRejectedValue(new Error('Service Unavailable (503)'))
    mount()
    await settle()
    expect(screen.getByText('Service Unavailable (503)')).toBeTruthy()
    // The rest row is still drawn, with no tip: neither "switch harness" nor "not yet".
    expect(screen.getByText('Rest of the context')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /more information/i })).toBeNull()
  })

  it('reads a picked past turn from the turn route, once per version of its row', async () => {
    const t = trace(3)
    const ours = { message: 10, memory: 0, rules: 0, skills: 0, other: 0 }
    // The gateway's rows: the chart's numbers only, no per-row detail.
    const slim: ContextTrace = { ...t, revision: 4, trend: [1, 2, 3].map(turn => ({ turn, ours, loop: 100 })) }
    traceMock.mockResolvedValue(slim)
    turnMock.mockResolvedValue({
      slot: SLOT,
      revision: 4,
      turn: { ...t.latest!, turn: 2, context_used: 7_777, context_window: 1_000_000, blocks: { your_message: 10 } },
    })
    mount()
    await settle()
    expect(turnMock).not.toHaveBeenCalled()
    await act(async () => { screen.getByRole('button', { name: /^Turn 2:/ }).click() })
    await settle()
    expect(turnMock).toHaveBeenCalledWith(SLOT, 2)
    expect(screen.getByTestId('whole-total').textContent).toBe('7,777')
    // Picking it again while its row is unchanged answers from cache.
    await act(async () => { screen.getByRole('button', { name: /^Turn 3:/ }).click() })
    await act(async () => { screen.getByRole('button', { name: /^Turn 2:/ }).click() })
    await settle()
    expect(turnMock).toHaveBeenCalledTimes(1)
  })

  it('re-reads a past turn sent again with the same numbers, by its row version', async () => {
    const t = trace(3)
    const ours = { message: 10, memory: 0, rules: 0, skills: 0, other: 0 }
    const rows = (version2: number) =>
      [1, 2, 3].map(turn => ({ turn, ours, loop: 100, used: 0, version: turn === 2 ? version2 : turn }))
    const read = (ttft: number) => ({
      slot: SLOT,
      revision: 4,
      turn: {
        ...t.latest!,
        turn: 2,
        context_used: 7_777,
        context_window: 1_000_000,
        loop_estimated: false,
        loop_chars: 50_000,
        engine: {
          total_chars: 52_000, model_calls: 1,
          categories: { history: 1_900, steering: 30_000, skillsIndex: 20_100 },
          percent: { history: 3.7, steering: 57.7, skillsIndex: 38.6 },
          counts: { skillsIndex: 28 }, parts: {}, window: null, media: null,
          history: { user: 1_900, assistant: 0, thinking: 0, compaction_summary: 0 },
          unknown_chars: 0, compacted: false, schema_known: true, approximate: false,
          crew_chars: 30, crew_split: { message: 30, memory: 0, rules: 0, skills: 0, other: 0 },
          user_other_chars: 0, mismatch_chars: 0,
        },
        ttft_ms: { total_ms: ttft, setup_ms: 0, model_ms: ttft },
      },
    })
    traceMock.mockResolvedValue({ ...t, revision: 4, trend: rows(2) })
    turnMock.mockResolvedValue(read(200))
    mount()
    await settle()
    await act(async () => { screen.getByRole('button', { name: /^Turn 2:/ }).click() })
    await settle()
    expect(turnMock).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).toMatch(/First reply after 0\.2\s?s/)
    // Turn 2 regenerated: the same counts, a new row version, a slower first reply.
    turnMock.mockResolvedValue(read(900))
    traceMock.mockResolvedValue({ ...t, revision: 5, trend: rows(4) })
    onUsageFrame(qc, frame(5))
    await window()
    await settle()
    expect(turnMock).toHaveBeenCalledTimes(2)
    expect(document.body.textContent).toMatch(/First reply after 0\.9\s?s/)
  })

  it('opens another session on its newest turn, not the pick made in the last one', async () => {
    const ours = { message: 10, memory: 0, rules: 0, skills: 0, other: 0 }
    const rows = [1, 2, 3, 4, 5, 6].map(turn => ({ turn, ours, loop: 100, used: 0, version: turn }))
    const a = { ...trace(6), revision: 4, trend: rows }
    const b = { ...trace(6), slot: 'chat-8', revision: 9, trend: rows }
    traceMock.mockImplementation((slot: string) => Promise.resolve(slot === SLOT ? a : b))
    turnMock.mockResolvedValue({ slot: SLOT, revision: 4, turn: { ...a.latest!, turn: 5 } })
    const view = mount()
    await settle()
    await act(async () => { screen.getByRole('button', { name: /^Turn 5:/ }).click() })
    await settle()
    expect(screen.getByTestId('show-latest')).toBeInTheDocument()
    view.rerender(createElement(QueryClientProvider, { client: qc }, createElement(ContextBreakdownTab, { slot: 'chat-8' })))
    await settle()
    expect(traceMock).toHaveBeenCalledWith('chat-8')
    expect(screen.queryByTestId('show-latest')).toBeNull()
    expect(screen.getByTestId('selected-turn-detail').textContent).toMatch(/Turn 6/)
    expect(turnMock).toHaveBeenCalledTimes(1)
  })

  it('keeps a picked past turn on screen, disclosures open, across new revisions', async () => {
    const t = trace(3)
    const ours = { message: 10, memory: 0, rules: 0, skills: 0, other: 0 }
    const rows = (n: number, loop2 = 100) =>
      Array.from({ length: n }, (_, i) => ({ turn: i + 1, ours, loop: i + 1 === 2 ? loop2 : 100, used: 0 }))
    const read = (used: number) => ({
      slot: SLOT,
      revision: 4,
      turn: { ...t.latest!, turn: 2, context_used: used, context_window: 1_000_000, blocks: { your_message: 10, lessons: 50 } },
    })
    traceMock.mockResolvedValue({ ...t, revision: 4, trend: rows(3) })
    turnMock.mockResolvedValue(read(7_777))
    mount()
    await settle()
    await act(async () => { screen.getByRole('button', { name: /^Turn 2:/ }).click() })
    await settle()
    await act(async () => { screen.getByRole('button', { name: /blocks sent this turn/ }).click() })
    expect(document.querySelectorAll('[data-block]')).toHaveLength(2)

    // A new turn elsewhere: turn 2's row is unchanged, so nothing is re-read or reset.
    traceMock.mockResolvedValue({ ...t, revision: 5, trend: rows(4), latest: { ...t.latest!, turn: 4 } })
    onUsageFrame(qc, frame(5))
    await window()
    expect(turnMock).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('whole-total').textContent).toBe('7,777')
    expect(document.querySelectorAll('[data-block]')).toHaveLength(2)

    // Turn 2 sent again: its row changed, so it is re-read, and the previous read stays
    // on screen with its disclosure open until the new one lands.
    const next = deferred<ReturnType<typeof read>>()
    turnMock.mockReturnValue(next.promise)
    traceMock.mockResolvedValue({ ...t, revision: 6, trend: rows(4, 300), latest: { ...t.latest!, turn: 4 } })
    onUsageFrame(qc, frame(6))
    await window()
    expect(turnMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('past-turn-read')).toBeNull()
    expect(screen.getByTestId('whole-total').textContent).toBe('7,777')
    expect(document.querySelectorAll('[data-block]')).toHaveLength(2)
    await act(async () => { next.resolve(read(8_888)) })
    await settle()
    expect(screen.getByTestId('whole-total').textContent).toBe('8,888')
    expect(document.querySelectorAll('[data-block]')).toHaveLength(2)
  })

  it('re-reads on every mount, even over a cached trace', async () => {
    traceMock.mockResolvedValue(trace(0))
    const first = mount()
    await settle()
    first.unmount()
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('does nothing for a slot no tab has read', async () => {
    expect(onUsageFrame(qc, frame(1, { slot: 'dashboard:other' }))).toBe('requested')
    await window()
    expect(traceMock).not.toHaveBeenCalled()
  })
})
