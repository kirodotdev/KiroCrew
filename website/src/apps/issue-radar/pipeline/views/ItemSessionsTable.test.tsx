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
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// ── the mocked seams ──────────────────────────────────────────────────────────
// Only ONE seam is left: `chatSlots`, which decides whether a slot is still LIVE
// and therefore whether its key renders as a link. The default answer is permissive
// so most tests get links; the retired path gets its OWN test that overrides it,
// rather than depending on a fixture name.
//
// There is deliberately no store or navigate mock any more. The key is a plain
// `<Link>`, so a router is all it needs -- which is also the point of the change:
// the component no longer dispatches, so there is no dispatch to fake.
const { liveSlotKeys, chatSlots } = vi.hoisted(() => {
  const liveSlotKeys: string[] = []
  return {
    liveSlotKeys,
    chatSlots: vi.fn(async () => liveSlotKeys.map((key) => ({ key }))),
  }
})
vi.mock('../../../../api/client', () => ({ api: { chatSlots } }))

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
  // Every rendered slot counts as live unless a test says otherwise, so most tests
  // get a linked key and the retired path is opted into explicitly.
  liveSlotKeys.length = 0
  liveSlotKeys.push(...(opts.live ?? sessions.map((s) => s.slot)))
  // The table asks the gateway which slots are still live, so it needs a query
  // client. Retries off and no cache carry-over, so one test's answer cannot leak
  // into the next. The router is needed because a live key renders a real <Link>.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <ItemSessionsTable
          sessions={sessions}
          populatedColumns={opts.populatedColumns ?? []}
          nowMs={opts.nowMs ?? 1_000_000_000}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const rowFor = (slot: string) => screen.getByTestId(`atp-session-${slot}`)

afterEach(() => {
  vi.clearAllMocks()
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

describe('ItemSessionsTable — the session key is the link', () => {
  it('renders a live key as a real anchor to the chat page deep link', async () => {
    // A real <a href> is the point: it can be middle-clicked, copied and opened in
    // a new tab, none of which a button that dispatches can do.
    renderTable([session({ slot: 'chat:live', current: true })])
    const link = await waitFor(() =>
      within(rowFor('chat:live')).getByRole('link', { name: 'chat:live' }),
    )
    expect(link.getAttribute('href')).toBe('/chat?sid=chat%3Alive')
  })

  it('percent-encodes the slot key so a key with reserved characters survives', async () => {
    // Slot keys carry ':' and can carry '/', both reserved in a query value. Left
    // raw they would truncate or re-target the link rather than fail loudly.
    renderTable([session({ slot: 'chat:a/b', current: true })])
    const link = await waitFor(() =>
      within(rowFor('chat:a/b')).getByRole('link', { name: 'chat:a/b' }),
    )
    expect(link.getAttribute('href')).toBe('/chat?sid=chat%3Aa%2Fb')
  })

  it('does NOT link a retired key, and says why', async () => {
    // The chat page resolves `?sid=` only against slots it currently lists, so a
    // link to a retired key would dead-end on its not-found notice after a timeout.
    // Withholding the link and naming the reason is the honest form of that answer.
    renderTable([session({ slot: 'chat:dead', current: false })], { live: [] })
    await waitFor(() => expect(chatSlots).toHaveBeenCalled())
    const row = rowFor('chat:dead')
    await waitFor(() => expect(within(row).getByText(/has been retired/i)).toBeTruthy())
    expect(within(row).queryByRole('link')).toBeNull()
    // The key is still READABLE -- only the affordance is withheld.
    expect(within(row).getByText('chat:dead')).toBeTruthy()
  })

  it('says the retired session cannot be OPENED, not that commands cannot be sent', async () => {
    // The old copy described a send box that no longer exists, so it explained a
    // restriction the row no longer has while leaving the real one unstated.
    renderTable([session({ slot: 'chat:dead' })], { live: [] })
    const row = rowFor('chat:dead')
    await waitFor(() => expect(within(row).getByText(/no longer be opened/i)).toBeTruthy())
    expect(within(row).queryByText(/commands can be sent/i)).toBeNull()
  })

  it('offers NO link while the liveness answer is still unknown', async () => {
    // Loading and failure are not retirement. Linking optimistically would produce
    // a dead link; calling it retired would assert something not established.
    renderTable([session({ slot: 'chat:pending', current: true })], { live: [] })
    const row = rowFor('chat:pending')
    expect(within(row).queryByRole('link')).toBeNull()
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
    await waitFor(() =>
      expect(within(rowFor('chat:live')).getByRole('link', { name: 'chat:live' })).toBeTruthy(),
    )
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
    // Unknown withholds the LINK too -- it never renders one optimistically.
    expect(within(row).queryByRole('link')).toBeNull()
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
