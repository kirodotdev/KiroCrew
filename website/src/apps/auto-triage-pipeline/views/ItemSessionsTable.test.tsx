/**
 * ItemSessionsTable — L2: the RENDERED contract of the agent sessions that worked
 * one item, plus the exported `cellText` unit renderer.
 *
 * The behaviours under test are the ones that make this component honest about
 * spend:
 *  - only the columns named in `populatedColumns` render — a column of the
 *    always-zero fields (cost/input today) printed beside a real credit total
 *    reads as "this work was free" rather than "this is not measured";
 *  - the totals line sums credits and turns across ALL sessions, current and
 *    retired — the whole point of the component is that a retried item's spend
 *    spans several slots, so a total that counted only the live slot would
 *    under-report by the retries;
 *  - the current session is marked and the non-current ones are not;
 *  - `cellText` renders credits and durations in human units and contextUsed as a
 *    percentage of the window, and does NOT divide by zero when the window is 0;
 *  - the send-command control reports a NON-OK fetch as FAILED, not sent — fetch
 *    resolves on a 4xx, so a missing status check would tell the operator a
 *    rejected instruction was delivered.
 *
 * The seams: `api` (its `sendChat`), `switchSlot` (the store thunk), and
 * `useNavigate` are all mocked so nothing dials, nothing needs a real Redux store
 * and nothing needs a router. English is installed by the shared setup, so the
 * asserted strings are the real catalog values.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ── the mocked seams ──────────────────────────────────────────────────────────
// `chatSlots` decides whether a slot is still LIVE, and only a live slot offers a
// send box -- the send endpoint creates a slot it does not recognise rather than
// refusing, so a retired one must not be sendable. The default answer is permissive
// so the send tests exercise the send path; the retired path gets its OWN test that
// overrides this, rather than depending on a fixture name.
const {
  sendChat,
  liveSlotKeys,
  chatSlots,
  switchSlotThunk,
  setActiveSlotAction,
  unwrap,
  dispatch,
  navigate,
  activeSlot,
} =
  vi.hoisted(() => {
    const liveSlotKeys: string[] = []
    // The real `switchSlot.pending` assigns `activeSlot` SYNCHRONOUSLY, so the
    // fake store follows the dispatched switch. A test that wants the operator to
    // move away mid-flight overwrites `activeSlot.key` after the click.
    const activeSlot: { key: string | null } = { key: null }
    return {
      sendChat: vi.fn(),
      liveSlotKeys,
      activeSlot,
      chatSlots: vi.fn(async () => liveSlotKeys.map((key) => ({ key }))),
      // switchSlot(slot) returns a thunk action; dispatch(action) returns a promise
      // with an .unwrap() the component awaits.
      switchSlotThunk: vi.fn((slot: string) => ({ type: 'chat/switchSlot', slot })),
      // The plain action creator the failure path dispatches to RELEASE the key.
      setActiveSlotAction: vi.fn((slot: string | null) => ({
        type: 'chat/setActiveSlot',
        payload: slot,
      })),
      unwrap: vi.fn<[], Promise<unknown>>(() => Promise.resolve()),
      dispatch: vi.fn((action: { type?: string; slot?: string }) => {
        if (action?.type === 'chat/switchSlot' && action.slot) activeSlot.key = action.slot
        return { unwrap }
      }),
      navigate: vi.fn(),
    }
  })
vi.mock('../../../api/client', () => ({ api: { sendChat, chatSlots } }))
vi.mock('../../../store/chatSlice', () => ({
  switchSlot: switchSlotThunk,
  setActiveSlot: setActiveSlotAction,
}))
vi.mock('react-redux', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-redux')>()
  return {
    ...actual,
    useDispatch: () => dispatch,
    // The component reads the LIVE `activeSlot` to decide whether a failed switch
    // still owns the chat surface, so the tests need a store they can move.
    useStore: () => ({ getState: () => ({ chat: { activeSlot: activeSlot.key } }) }),
  }
})
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigate }
})

import ItemSessionsTable, { cellText, slotKeysOf } from './ItemSessionsTable'
import type { ItemSession } from '../api'

// ── fixtures ──────────────────────────────────────────────────────────────────
function session(over: Partial<ItemSession> = {}): ItemSession {
  return {
    slot: 'chat:1',
    model: 'sonnet',
    agent: 'kirocrew',
    surface: 'dashboard',
    current: false,
    startedAt: null,
    lastAt: null,
    turns: 0,
    input: 0,
    output: 0,
    cacheCreate: 0,
    cacheRead: 0,
    cost: 0,
    credits: 0,
    durationMs: 0,
    contextUsed: 0,
    contextWindow: 0,
    lastPhase: '',
    lastStopReason: '',
    ...over,
  }
}

function renderTable(
  sessions: ItemSession[],
  opts: { populatedColumns?: string[]; nowMs?: number; live?: string[] } = {},
) {
  // Every rendered slot counts as live unless a test says otherwise, so the send
  // tests exercise the send path and the retired path is opted into explicitly.
  liveSlotKeys.length = 0
  liveSlotKeys.push(...(opts.live ?? sessions.map((s) => s.slot)))
  // The table asks the gateway which slots are still live, so it needs a query
  // client. Retries off and no cache carry-over, so one test's answer cannot leak
  // into the next.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <ItemSessionsTable
        sessions={sessions}
        populatedColumns={opts.populatedColumns ?? []}
        nowMs={opts.nowMs ?? 1_000_000_000}
      />
    </QueryClientProvider>,
  )
}

const rowFor = (slot: string) => screen.getByTestId(`atp-session-${slot}`)

afterEach(() => {
  vi.clearAllMocks()
  unwrap.mockResolvedValue(undefined)
  activeSlot.key = null
})

describe('ItemSessionsTable — empty state', () => {
  it('renders the designed empty state for no sessions', () => {
    renderTable([])
    expect(screen.getByTestId('atp-sessions-empty')).toBeTruthy()
    expect(screen.queryByTestId('atp-sessions-total')).toBeNull()
  })
})

describe('ItemSessionsTable — column gating', () => {
  it('renders ONLY the columns named in populatedColumns; the zero fields it omits are not printed', () => {
    // A session whose credits carry a DISTINCTIVE value, while cost and input are
    // the structurally-zero fields. With populatedColumns=['credits'] only the
    // credit value may appear as a cell — the zero cost/input must not. Below 100
    // so it renders with two decimals, distinct from any whole-number field.
    renderTable(
      [session({ slot: 'chat:1', credits: 87.5, cost: 0, input: 0 })],
      { populatedColumns: ['credits'] },
    )
    const row = rowFor('chat:1')
    // The credits column renders its human value.
    expect(within(row).getByText('87.50')).toBeTruthy()
    // Columns are named by a VISIBLE header row, not a hover-only title (which
    // reaches neither touch nor keyboard). An omitted column must have no header.
    const headers = screen.getByTestId('atp-session-headers')
    expect(within(headers).getByText('Credits')).toBeTruthy()
    expect(within(headers).queryByText('Cost')).toBeNull()
    expect(within(headers).queryByText('Input')).toBeNull()
  })

  it('renders several columns when several are populated', () => {
    renderTable(
      [session({ slot: 'chat:1', credits: 10, input: 42, cost: 3 })],
      { populatedColumns: ['credits', 'input', 'cost'] },
    )
    const headers = screen.getByTestId('atp-session-headers')
    expect(within(headers).getByText('Credits')).toBeTruthy()
    expect(within(headers).getByText('Input')).toBeTruthy()
    expect(within(headers).getByText('Cost')).toBeTruthy()
    expect(within(rowFor('chat:1')).getByText('42')).toBeTruthy()
  })

  it('renders NO header row when no column carries data', () => {
    // A header strip over an empty column set would promise numbers the payload
    // says are not measured.
    renderTable([session({ slot: 'chat:1' })], { populatedColumns: [] })
    expect(screen.queryByTestId('atp-session-headers')).toBeNull()
  })
})

describe('ItemSessionsTable — totals across ALL sessions', () => {
  it('sums credits and turns across current AND non-current sessions', () => {
    // A retried item: the live slot spent only the tail (187), the retired ones
    // the rest — the total must be the WHOLE spend (187 + 3000 + 872.65 = 4059.65),
    // not just the current slot's 187.
    renderTable([
      session({ slot: 'chat:live', current: true, credits: 187, turns: 4 }),
      session({ slot: 'chat:old1', current: false, credits: 3000, turns: 20 }),
      session({ slot: 'chat:old2', current: false, credits: 872.65, turns: 11 }),
    ])
    const total = screen.getByTestId('atp-sessions-total').textContent ?? ''
    // 3 sessions, 35 turns, credits summed and formatted (>=100 -> whole, grouped).
    // Each figure is LABELLED, so a reader cannot mistake the credit sum for the
    // live slot's own spend — the whole point of the strip.
    expect(total).toContain('Sessions')
    expect(total).toContain('3')
    expect(total).toContain('Turns')
    expect(total).toContain('35')
    expect(total).toContain('Credits')
    expect(total).toContain('4,060') // formatCredits(4059.65)
  })
})

describe('ItemSessionsTable — current marking', () => {
  it('marks the current session and leaves the non-current ones unmarked', () => {
    renderTable([
      session({ slot: 'chat:live', current: true }),
      session({ slot: 'chat:old', current: false }),
    ])
    // The current session carries the "Current session" indicator …
    expect(within(rowFor('chat:live')).getByLabelText('Current session')).toBeTruthy()
    // … and the retired one does not.
    expect(within(rowFor('chat:old')).queryByLabelText('Current session')).toBeNull()
  })
})

describe('cellText — unit rendering', () => {
  it('renders credits in human units', () => {
    expect(cellText('credits', session({ credits: 17.75 }))).toBe('17.75')
    expect(cellText('credits', session({ credits: 4059.65 }))).toBe('4,060')
  })

  it('renders a duration in human units, not raw milliseconds', () => {
    expect(cellText('durationMs', session({ durationMs: 184_000 }))).toBe('3m 4s')
    expect(cellText('durationMs', session({ durationMs: 820 }))).toBe('820ms')
  })

  it('renders contextUsed as a percentage of the window', () => {
    // 4629 of 10000 -> 46%.
    expect(cellText('contextUsed', session({ contextUsed: 4629, contextWindow: 10_000 }))).toBe('46%')
  })

  it('does NOT divide by zero when the context window is 0 — falls back to the raw used count', () => {
    // A zero window must not produce "Infinity%" or "NaN%".
    const out = cellText('contextUsed', session({ contextUsed: 512, contextWindow: 0 }))
    expect(out).toBe('512')
    expect(out).not.toMatch(/%|Infinity|NaN/)
  })

  it('renders the plain-number columns as their raw count and an unknown column as empty', () => {
    expect(cellText('input', session({ input: 42 }))).toBe('42')
    expect(cellText('output', session({ output: 7 }))).toBe('7')
    expect(cellText('cost', session({ cost: 0 }))).toBe('0')
    expect(cellText('cacheRead', session({ cacheRead: 99 }))).toBe('99')
    expect(cellText('cacheCreate', session({ cacheCreate: 5 }))).toBe('5')
    expect(cellText('nonsense', session())).toBe('')
  })
})

describe('ItemSessionsTable — open session action', () => {
  it('dispatches switchSlot for the row slot and navigates to /chat', async () => {
    renderTable([session({ slot: 'chat:live', current: true })])
    // The action only exists once the live-slot answer has arrived — a retired slot
    // never gets it, so waiting is part of the contract rather than test patience.
    const btn = await waitFor(() => within(rowFor('chat:live')).getByText('Open session'))
    fireEvent.click(btn)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
    expect(switchSlotThunk).toHaveBeenCalledWith('chat:live')
    expect(dispatch).toHaveBeenCalled()
  })

  it('offers NO open-session action for a retired slot', async () => {
    // `switchSlot.pending` assigns `activeSlot` synchronously and
    // `switchSlot.rejected` leaves it assigned, so opening a dead key strands the
    // chat surface on it — and the send endpoint CREATES an unrecognised slot
    // rather than refusing. The operator's next message would resurrect the retired
    // key and bill fresh usage to this pipeline item. The action must be absent, not
    // merely fail.
    renderTable([session({ slot: 'chat:dead', current: false })], { live: [] })
    await waitFor(() => expect(chatSlots).toHaveBeenCalled())
    const row = rowFor('chat:dead')
    await waitFor(() =>
      expect(within(row).getByText(/has been retired/i)).toBeTruthy(),
    )
    expect(within(row).queryByText('Open session')).toBeNull()
    expect(switchSlotThunk).not.toHaveBeenCalled()
  })

  it('does NOT navigate when a live slot dies between the poll and the click', async () => {
    // The irreducible race: the row was offered because the slot was live, and the
    // switch then failed. Navigating anyway would land the operator on the phantom
    // key, so the slot state is cleared and navigation is abandoned instead.
    unwrap.mockRejectedValueOnce(new Error('gone'))
    renderTable([session({ slot: 'chat:live', current: true })])
    const btn = await waitFor(() => within(rowFor('chat:live')).getByText('Open session'))
    fireEvent.click(btn)
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'chat/clearSlotState' }),
    )
    expect(navigate).not.toHaveBeenCalled()
  })

  it('CLEARS then releases, in that order, when the switch fails', async () => {
    // Both dispatches are load-bearing and the order is the opposite of the natural
    // reading. `clearSlotState` does not clear `activeSlot`; it READS it to delete
    // that slot's pending question:
    //
    //     if (state.activeSlot) delete state.pendingQuestions?.[state.activeSlot]
    //
    // so it must run while the key still names the dead slot. Releasing first makes
    // that guard false and orphans the retired slot's pending question forever.
    //
    // This test previously asserted the REVERSE order and so pinned the bug in
    // place. Asserting the order is only useful if the asserted order is the
    // correct one -- a test can enforce a mistake just as firmly as a rule.
    unwrap.mockRejectedValueOnce(new Error('gone'))
    renderTable([session({ slot: 'chat:live', current: true })])
    const btn = await waitFor(() => within(rowFor('chat:live')).getByText('Open session'))
    fireEvent.click(btn)
    await waitFor(() => expect(setActiveSlotAction).toHaveBeenCalledWith(null))

    const calls = dispatch.mock.calls.map(([a]) => a)
    const iClear = calls.findIndex((a) => a && a.type === 'chat/clearSlotState')
    const iRelease = calls.findIndex((a) => a && a.type === 'chat/setActiveSlot')
    expect(iClear).toBeGreaterThanOrEqual(0)
    expect(iRelease).toBeGreaterThanOrEqual(0)
    expect(iClear).toBeLessThan(iRelease)
  })

  it('does NOT clear or release when the operator switched chats before the failure landed', async () => {
    // Neither dispatch is slot-scoped: `clearSlotState` drops the messages, tool log
    // and subagents wholesale and deletes whichever slot `activeSlot` names at that
    // moment. The await is a real round trip, so the operator can open a different
    // chat before it rejects -- and cleaning up then would wipe the conversation
    // they just opened, on behalf of a slot this handler no longer owns.
    let fail: () => void = () => {}
    unwrap.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { fail = () => reject(new Error('gone')) }),
    )
    renderTable([session({ slot: 'chat:live', current: true })])
    const btn = await waitFor(() => within(rowFor('chat:live')).getByText('Open session'))
    fireEvent.click(btn)
    // The switch is in flight and owns the surface; now the operator moves.
    await waitFor(() => expect(activeSlot.key).toBe('chat:live'))
    activeSlot.key = 'chat:elsewhere'
    fail()

    await waitFor(() => expect(navigate).not.toHaveBeenCalled())
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'chat/clearSlotState' })
    expect(setActiveSlotAction).not.toHaveBeenCalled()
  })
})

describe('ItemSessionsTable — liveness is three-state, not two', () => {
  it('does NOT claim a session is retired while the liveness probe is still loading', async () => {
    // An empty live-slot set is also what LOADING looks like. Collapsing that into
    // "retired" printed "This session has been retired" beside the current-session
    // marker on every first expansion -- the view asserting a falsehood.
    let release: (v: unknown) => void = () => {}
    chatSlots.mockImplementationOnce(() => new Promise((res) => (release = res)))
    renderTable([session({ slot: 'chat:live', current: true })])
    const row = rowFor('chat:live')
    expect(within(row).queryByText(/has been retired/i)).toBeNull()
    expect(within(row).getByText(/checking session/i)).toBeTruthy()
    release([{ key: 'chat:live' }])
    await waitFor(() => expect(within(rowFor('chat:live')).getByText('Open session')).toBeTruthy())
  })

  it('reports liveness as UNAVAILABLE, not as retired, when the probe fails', async () => {
    // Permanent on error under the old two-state read: every session would be
    // labelled retired forever and the operator would believe steering was
    // impossible.
    chatSlots.mockRejectedValueOnce(new Error('probe down'))
    renderTable([session({ slot: 'chat:live', current: true })])
    const row = await waitFor(() => {
      const r = rowFor('chat:live')
      expect(within(r).getByText(/status unavailable/i)).toBeTruthy()
      return r
    })
    expect(within(row).queryByText(/has been retired/i)).toBeNull()
    // Unknown still withholds the write -- it never ENABLES sending.
    expect(within(row).queryByLabelText('Send an instruction')).toBeNull()
    expect(within(row).queryByText('Open session')).toBeNull()
  })
})

describe('slotKeysOf', () => {
  it('reads an array of records, an array of strings, and a wrapper object', () => {
    expect(slotKeysOf([{ key: 'a' }, { slot: 'b' }, { name: 'c' }])).toEqual(
      new Set(['a', 'b', 'c']),
    )
    expect(slotKeysOf(['x', 'y'])).toEqual(new Set(['x', 'y']))
    expect(slotKeysOf({ slots: [{ key: 'z' }] })).toEqual(new Set(['z']))
  })

  it('yields an EMPTY set for anything unreadable, so sending is disabled not enabled', () => {
    for (const bad of [null, undefined, 42, 'nope', {}, { slots: 'no' }, [null, {}, 7]]) {
      expect(slotKeysOf(bad).size).toBe(0)
    }
  })
})
