/**
 * `wait` countdown row on the tool pill.
 *
 * The countdown is joined to the pill by TOOL NAME, not by id (the wait_id is
 * minted inside the MCP subprocess and never reaches the ACP tool_call frame),
 * and its deadline comes from the slots payload rather than the tool input. Both
 * choices are only safe while the four negative cases below hold — a non-`wait`
 * tool, a finished call, a slot with no sleep, and a zero deadline must all
 * render nothing — so they are asserted as carefully as the happy path.
 *
 * Time is pinned with fake timers: the label is derived from
 * `deadline_ts - Date.now()`, so a real clock makes every expected string a
 * moving target.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import { sseSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import type { RootState } from '../store'
import type { ChatMessage, ChatSlot } from '../types'

type ChatState = RootState['chat']
type WaitState = NonNullable<ChatSlot['wait_state']>

const SLOT = 'slot-wait'
const TOOL_CALL_ID = 'tc_wait'
const WAIT_ID = 'wait-abc123'

/** Fixed wall clock for every test here. Whole seconds so `deadline_ts`
 *  (absolute SECONDS) converts back to this exact millisecond value. */
const NOW_MS = 1_760_000_000_000
const NOW_S = NOW_MS / 1000

/** happy-dom has no ResizeObserver; the details panel's segmented control uses
 *  one to pick its layout. Same stub as ToolCallLine.test.tsx. */
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/** Pin the clock BEFORE render: `activityNow` seeds from `Date.now()` in a
 *  useState initializer, so a clock set afterwards would leave the first paint
 *  computed against the real time. */
function freezeClock() {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
}

function waitMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'tool', content: '🔧 wait', cls: '', meta: { tool_call_id: TOOL_CALL_ID }, ...overrides }
}

function waitStateAt(remainingSeconds: number, waitId = WAIT_ID): WaitState {
  return { wait_id: waitId, seconds: remainingSeconds, deadline_ts: NOW_S + remainingSeconds }
}

function makeStore({
  toolName = 'wait',
  output,
  waitState = waitStateAt(252),
  slotKey = SLOT,
  activeSlot = SLOT,
  messages = [waitMsg()],
  logEntry = true,
  slotRunning = true,
  payloadRunning = true,
}: {
  toolName?: string
  output?: string
  waitState?: WaitState | null
  slotKey?: string
  activeSlot?: string | null
  /** Transcript. Defaults to the single pill the tests above render. */
  messages?: ChatMessage[]
  /** false → an EMPTY runtime tool log: the shape after a mid-wait reload,
   *  where the pill survives only as a persisted tool message. */
  logEntry?: boolean
  slotRunning?: boolean
  /** The slots PAYLOAD's own `running` flag — distinct from `chat.slotRunning`.
   *  The wait selector reads this one, because a replayed row has no live log
   *  entry to infer liveness from. */
  payloadRunning?: boolean
} = {}) {
  return createTestStore({
    chat: {
      messages,
      // `output: undefined` is what keeps the call live — the memo selector reads
      // `e.output != null` as done. `input` is present so the details panel has
      // something to render, which is what test 6 checks the absence of.
      toolLog: logEntry ? [{
        type: 'tool',
        text: toolName,
        tool_call_id: TOOL_CALL_ID,
        input: '{"seconds":300}',
        ...(output !== undefined ? { output } : {}),
        ts: 1,
      }] : [],
      slotRunning,
      activeSlot,
    } as unknown as ChatState,
    dashboard: {
      slots: [{ key: slotKey, messages: 1, running: payloadRunning, wait_state: waitState }],
      unreadSlots: [],
      refreshTrigger: 0,
      connected: true,
    } as unknown as RootState['dashboard'],
  })
}

function renderWait(store = makeStore(), message: ChatMessage = waitMsg()) {
  return renderWithProviders(<ToolCallLine message={message} running />, { store })
}

const row = () => screen.getByTestId('wait-countdown')
const endBtn = () => screen.getByTestId('wait-end-now') as HTMLButtonElement
/** The pill itself — its aria-label carries Show/Hide, its aria-expanded the state. */
const pill = () => screen.getByLabelText(/details for tool/)

describe('ToolCallLine wait countdown — rendering and ticking', () => {
  it('renders the countdown row and decrements it once per second', () => {
    freezeClock()
    // 252s = 4m 12s. Chosen so both components are non-zero and the seconds
    // place is two digits — a label that dropped minutes or seconds is visible.
    renderWait()

    expect(row().textContent).toContain('Waiting')
    expect(row().textContent).toContain('4m 12s left')

    act(() => { vi.advanceTimersByTime(1000) })
    expect(row().textContent).toContain('4m 11s left')
    expect(row().textContent).not.toContain('4m 12s')
  })

  it('drops the minutes component below 60s', () => {
    freezeClock()
    renderWait(makeStore({ waitState: waitStateAt(45) }))

    expect(row().textContent).toContain('45s left')
    // No minutes anywhere in the row — not "0m 45s", not "0m".
    expect(row().textContent).not.toMatch(/\dm/)
  })

  it('keeps the seconds place at the 60s boundary rather than reading a bare "1m"', () => {
    freezeClock()
    renderWait(makeStore({ waitState: waitStateAt(60) }))

    // The step is 1m 1s → 1m 0s → 59s. A dropped zero would read "1m" here and
    // look frozen for a tick.
    expect(row().textContent).toContain('1m 0s left')

    act(() => { vi.advanceTimersByTime(1000) })
    expect(row().textContent).toContain('59s left')
  })

  it('clamps at zero instead of counting into negatives', () => {
    freezeClock()
    // The sleep can outlast its own deadline by up to one poll interval, so a
    // past deadline is a NORMAL state, not a corrupt one.
    renderWait(makeStore({ waitState: waitStateAt(-30) }))

    expect(row().textContent).toContain('0s left')
    // Neither an ASCII hyphen nor Intl's U+2212 minus sign may appear.
    expect(row().textContent).not.toContain('-')
    expect(row().textContent).not.toContain('\u2212')

    // Still parked at 0s a second later — the clamp is not a one-frame accident.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(row().textContent).toContain('0s left')
    expect(row().textContent).not.toContain('-')
  })
})

describe('ToolCallLine wait countdown — transport-shaped tool titles', () => {
  // The title on the transcript's tool entry is decided by the TRANSPORT, not by
  // the tool: `wait` direct over MCP, `kirocrew-core___wait` through the pooled
  // gateway's namespacing, `wait (mcp)` where a suffix is appended. The
  // `toolName === 'wait'` check this replaced matched only the first, so on the
  // other two the countdown never appeared at all.
  for (const toolName of ['wait', 'kirocrew-core___wait', 'wait (mcp)']) {
    it(`renders the countdown when the log entry's title reads "${toolName}"`, () => {
      freezeClock()
      renderWait(makeStore({ toolName }))

      expect(row().textContent).toContain('Waiting')
      expect(row().textContent).toContain('4m 12s left')
      expect(endBtn().disabled).toBe(false)
    })
  }

  it('still ignores a title that merely contains the letters', () => {
    freezeClock()
    // Control for the loop above: it must be passing because those three forms
    // MATCH, not because the name check was widened into nothing. `awaiting`
    // shares the substring and produces no `wait` token.
    renderWait(makeStore({ toolName: 'awaiting_input' }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })
})

describe('ToolCallLine wait countdown — after a mid-wait page reload', () => {
  // A reload empties the runtime tool log. The pill comes back as a persisted
  // tool message only, and the selector's historical branch then defaults
  // `isDone` to true — a REPLAY DEFAULT, not an observation that the call
  // finished. Gating the countdown on it blanked the row in exactly the case the
  // server-side deadline exists to serve, so liveness is read from
  // `slot.wait_state` instead and `isDone` is honoured only while a real log
  // entry backs the row.
  it('renders the countdown from slot.wait_state with no tool log entry at all', () => {
    freezeClock()
    const store = makeStore({ logEntry: false })
    // The fixture IS the reload shape — assert it rather than trusting the flag.
    expect(store.getState().chat.toolLog).toEqual([])

    renderWait(store)

    expect(row().textContent).toContain('Waiting')
    expect(row().textContent).toContain('4m 12s left')

    // Live, not one frozen paint: it keeps ticking off the server deadline.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(row().textContent).toContain('4m 11s left')
  })

  it('keeps the End wait button working on a reloaded pill', () => {
    freezeClock()
    const spy = vi.spyOn(api, 'endWait').mockResolvedValue({ ok: true })
    renderWait(makeStore({ logEntry: false }))

    expect(endBtn().disabled).toBe(false)
    fireEvent.click(endBtn())

    // The wait_id rides on the slot too, so a row that never saw the sleep start
    // can still address it.
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(SLOT, WAIT_ID)
    expect(endBtn().disabled).toBe(true)
    expect(endBtn().textContent).toContain('Ending')
  })

  it('renders nothing when the reloaded slot is not sleeping', () => {
    freezeClock()
    // The complement, and the reason dropping the unconditional `isDone` gate is
    // safe: with no `wait_state` the same historical pill is inert, so a replayed
    // wait in an old transcript stays silent.
    renderWait(makeStore({ logEntry: false, waitState: null }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
    expect(screen.queryByTestId('wait-end-now')).toBeNull()
  })
})

describe('ToolCallLine wait countdown — only the newest wait owns the countdown', () => {
  // Consequence of the rule above: a replayed wait is no longer inert on its own,
  // so a transcript holding SEVERAL waits and one sleeping slot would light every
  // one of them. Exactly one row may own the countdown — the last wait message in
  // the transcript. Each pill is rendered on its own (one mount per test) so the
  // testid queries stay unambiguous.
  const otherWait = () => waitMsg({ meta: { tool_call_id: 'tc_wait_other' } })
  const thisWait = () => waitMsg() // TOOL_CALL_ID

  it('renders on the LAST wait message in the transcript', () => {
    freezeClock()
    renderWait(makeStore({ logEntry: false, messages: [otherWait(), thisWait()] }), thisWait())

    expect(row().textContent).toContain('4m 12s left')
  })

  it('renders nothing on an EARLIER wait in the same transcript', () => {
    freezeClock()
    renderWait(makeStore({ logEntry: false, messages: [otherWait(), thisWait()] }), otherWait())

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
    expect(screen.queryByTestId('wait-end-now')).toBeNull()
  })

  it('follows transcript POSITION, not the tool_call_id', () => {
    freezeClock()
    // The same two pills, reversed. If ownership were keyed on anything but
    // position — the fixture's own id, insertion order of the slot payload — the
    // countdown would stay on TOOL_CALL_ID instead of moving here.
    renderWait(makeStore({ logEntry: false, messages: [thisWait(), otherWait()] }), otherWait())

    expect(row().textContent).toContain('4m 12s left')
  })

  it('goes quiet on the pill that was newest before the transcript order changed', () => {
    freezeClock()
    renderWait(makeStore({ logEntry: false, messages: [thisWait(), otherWait()] }), thisWait())

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('yields the countdown once ANY later tool call exists', () => {
    freezeClock()
    // The scan stops at the transcript's newest TOOL row of any kind, so a wait
    // followed by other tool activity stops owning the countdown.
    //
    // That is not a limitation, it is the point. Within one ACP session the agent
    // is blocked on the wait tool's result and cannot issue another call, so a
    // live wait is ALWAYS the newest tool row. A later tool row therefore means
    // one of two things, and the countdown is wrong in both: this wait is over,
    // or the `wait_state` belongs to a different ACP session sharing this slot —
    // which is real, because `_resolve_session_key()` answers per runtime, not
    // per session. Skipping past the later row would paint a subagent's remaining
    // time onto the parent's spent pill.
    const transcript: ChatMessage[] = [
      thisWait(),
      { role: 'assistant', content: 'Polling the run…', cls: '' },
      { role: 'tool', content: '🔧 Running: gh pr checks', cls: '', meta: { tool_call_id: 'tc_shell' } },
    ]
    renderWait(makeStore({ logEntry: false, messages: transcript }), thisWait())

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('keeps the countdown when only non-tool rows follow the wait', () => {
    freezeClock()
    // The complement of the case above: assistant/user rows are skipped, because
    // streamed prose after the tool_call frame is the normal shape while a wait is
    // in flight. Only a later TOOL row transfers ownership.
    const transcript: ChatMessage[] = [
      thisWait(),
      { role: 'assistant', content: 'Polling the run…', cls: '' },
      { role: 'user', content: 'thanks', cls: '' },
    ]
    renderWait(makeStore({ logEntry: false, messages: transcript }), thisWait())

    expect(row().textContent).toContain('4m 12s left')
  })

  it('renders nothing while the slot is not running', () => {
    freezeClock()
    // A replayed row cannot observe liveness — the historical branch defaults
    // `isDone` to true and is deliberately not trusted — so the slot's own
    // `running` flag is what stops an idle tab counting down against a deadline
    // the backend is tracking for something else.
    renderWait(
      makeStore({ logEntry: false, messages: [thisWait()], payloadRunning: false }),
      thisWait(),
    )

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })
})

describe('ToolCallLine wait countdown — negative cases', () => {
  it('renders nothing for a non-wait tool even when the slot is sleeping', () => {
    freezeClock()
    // The slot HAS a wait_state — only the tool name differs. Guards the
    // name-based join against lighting up every running pill in the transcript.
    renderWait(makeStore({ toolName: 'shell' }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
    expect(screen.queryByTestId('wait-end-now')).toBeNull()
  })

  it('renders nothing once the wait tool has produced output', () => {
    freezeClock()
    // A LIVE log entry carrying an output — the half of the `isDone` gate that
    // survives: `fromLog && isDone`. The historical branch's `isDone` is a replay
    // default and is deliberately NOT trusted (see the reload suite above).
    renderWait(makeStore({ output: 'Waited 300 seconds' }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('renders nothing when the slot carries no wait_state', () => {
    freezeClock()
    renderWait(makeStore({ waitState: null }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('renders nothing when deadline_ts is 0', () => {
    freezeClock()
    // A wait_state with no usable deadline (pre-deadline backend, or a payload
    // mid-transition) must not render a countdown that reads "0s" forever.
    renderWait(makeStore({ waitState: { wait_id: WAIT_ID, seconds: 300, deadline_ts: 0 } }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('renders nothing when no slot matches the active key', () => {
    freezeClock()
    // Complement to the wait_state cases: the lookup is by key, so a slots
    // payload that does not contain this slot must be treated as "not sleeping".
    renderWait(makeStore({ slotKey: 'some-other-slot' }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('renders nothing while a live log entry reports the turn already finished', () => {
    freezeClock()
    // The other half of the conditional `isDone` gate, reached through
    // `!slotRunning` rather than through an output: while a real log entry backs
    // the row, `isDone` is a genuine observation and is still honoured — a wait
    // that ends in a live session stops ticking on the tool result, not one poll
    // interval later when the slots push clears wait_state.
    renderWait(makeStore({ slotRunning: false }))

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
  })

  it('renders nothing for a non-wait historical row even though the slot is sleeping', () => {
    freezeClock()
    // Complement to the live non-wait case above, for the reload path: with no
    // log entry the message CONTENT is the only carrier of the tool name, and it
    // still has to gate the countdown. Otherwise every replayed pill in a
    // reloaded transcript would show a countdown while any wait is sleeping.
    const shell = waitMsg({ content: '🔧 Running: gh pr checks' })
    renderWait(makeStore({ logEntry: false, messages: [shell] }), shell)

    expect(screen.queryByTestId('wait-countdown')).toBeNull()
    expect(screen.queryByTestId('wait-end-now')).toBeNull()
  })
})

describe('ToolCallLine wait countdown — End wait button', () => {
  it('calls api.endWait once with (slot, wait_id) and latches the button', async () => {
    freezeClock()
    const spy = vi.spyOn(api, 'endWait').mockResolvedValue({ ok: true })
    renderWait()

    expect(endBtn().textContent).toContain('End wait')
    expect(endBtn().disabled).toBe(false)

    fireEvent.click(endBtn())

    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(SLOT, WAIT_ID)
    expect(endBtn().disabled).toBe(true)
    expect(endBtn().textContent).toContain('Ending')
    expect(endBtn().textContent).not.toContain('End wait')

    // A second click while in flight must not issue a second request — the
    // backend answers a duplicate with 409.
    fireEvent.click(endBtn())
    await act(async () => {})
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('does not toggle the details panel (the click is stopped at the row)', () => {
    freezeClock()
    vi.spyOn(api, 'endWait').mockResolvedValue({ ok: true })
    renderWait()

    expect(pill().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: 'Input' })).toBeNull()

    fireEvent.click(endBtn())

    expect(pill().getAttribute('aria-expanded')).toBe('false')
    // The details panel's segment control is the observable proof it did not open.
    expect(screen.queryByRole('button', { name: 'Input' })).toBeNull()

    // Positive control: the panel CAN open, so its absence above is meaningful
    // rather than an artifact of this fixture never rendering details at all.
    fireEvent.click(pill())
    expect(pill().getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('button', { name: 'Input' })).toBeTruthy()
  })

  it('re-enables the button when the request fails', async () => {
    freezeClock()
    const spy = vi.spyOn(api, 'endWait').mockRejectedValue(new Error('network down'))
    renderWait()

    fireEvent.click(endBtn())
    expect(endBtn().disabled).toBe(true)

    // Flush the rejection handler.
    await act(async () => {})

    expect(endBtn().disabled).toBe(false)
    expect(endBtn().textContent).toContain('End wait')
    // Retryable: a second click reaches the api again.
    fireEvent.click(endBtn())
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('re-arms for a NEW sleep but stays latched for the one just ended', async () => {
    freezeClock()
    const spy = vi.spyOn(api, 'endWait').mockResolvedValue({ ok: true })
    const store = makeStore()
    renderWait(store)

    fireEvent.click(endBtn())
    await act(async () => {})
    // Deliberately still disabled: the ended row survives up to one poll
    // interval, and a live button there invites a 409.
    expect(endBtn().disabled).toBe(true)

    // A poll delivers a DIFFERENT sleep in the same slot.
    act(() => {
      store.dispatch(sseSlots([
        { key: SLOT, messages: 1, running: true, wait_state: waitStateAt(90, 'wait-second') },
      ] as unknown as ChatSlot[]))
    })

    expect(endBtn().disabled).toBe(false)
    expect(endBtn().textContent).toContain('End wait')
    expect(row().textContent).toContain('1m 30s left')

    fireEvent.click(endBtn())
    expect(spy).toHaveBeenLastCalledWith(SLOT, 'wait-second')
  })
})

describe('ToolCallLine wait countdown — accessibility', () => {
  it('announces the status once and hides the ticking digits from assistive tech', () => {
    freezeClock()
    renderWait()

    const live = row().querySelectorAll('[aria-live]')
    // Exactly one live region: a second one (or a live region wrapping the
    // digits) would re-announce every single second.
    expect(live.length).toBe(1)
    expect(live[0].textContent).toBe('Waiting')
    // The announced text carries no digits, so the 1Hz updates are silent.
    expect(live[0].textContent).not.toMatch(/\d/)

    const digits = [...row().querySelectorAll('span')].find(s => /4m 12s/.test(s.textContent || ''))
    expect(digits).toBeTruthy()
    expect(digits!.getAttribute('aria-hidden')).toBe('true')

    // The tick must not turn the digits into an announced update either.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(row().querySelectorAll('[aria-live]').length).toBe(1)
    expect(row().querySelector('[aria-live]')!.textContent).toBe('Waiting')
  })
})
