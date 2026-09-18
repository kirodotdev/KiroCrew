/**
 * Test: remote-instance live sessions become mergeable rows, and the preview flag
 * gates the WIRE rather than the render.
 *
 * The load-bearing properties are about blast radius and units, not shape:
 *  - flag OFF issues NO request at all. This hook runs inside `ChatSidebar`, which
 *    every dashboard user mounts, so a version that fetched and discarded rows
 *    would put every user's instances on the wire for an opt-in preview.
 *  - a DISCONNECTED instance is never queried (the proxy would 503 it).
 *  - one unreachable instance contributes no rows and lands in `failed`, while
 *    every other instance's rows still arrive — one dead tunnel cannot empty the
 *    list, which is the objection that sank an earlier fully-merged design.
 *  - the peer's ISO ladder (`last_turn_ts` / `last_ts` / `created`) is passed
 *    through VERBATIM and NOTHING is derived from it: `slotActivityTs` already
 *    collapses that ladder for ranking, the date header and the row label alike,
 *    so a derived epoch field would be a second definition of the same thing.
 *    Asserted through `lastActivityEpoch` — the function the list ranks on — so
 *    these cases pin the ORDERING the ladder produces rather than an intermediate
 *    field's existence. `last_message` is NOT a timestamp: it is an 80-char
 *    message preview, so synthesizing a sort key from it put a string where a
 *    number belongs and NaN-poisoned the whole merged list's sort.
 *  - the row carries `peer_id` / `peer_name` and NOT `instance_id`, which is what
 *    makes the sidebar's badge and remote-activation path fire. #7104's own spec
 *    asserts the badge's classes, so this one asserts the data that reaches it.
 *    The absence assertion is the load-bearing half: `instance_id` on a live
 *    `Slot` means a LOCAL session that DISPATCHES its turns to a peer, so a row
 *    carrying both would be read by ChatSidebar as remote-executed as well as
 *    peer-owned — and a peer slot that is itself bound to a THIRD machine would
 *    have that binding overwritten with the peer that merely answered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const { listInstancesMock, instanceChatSlotsMock } = vi.hoisted(() => ({
  listInstancesMock: vi.fn(),
  instanceChatSlotsMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { listInstances: listInstancesMock, instanceChatSlots: instanceChatSlotsMock },
}))

import { useInstanceSessions } from '../hooks/useInstanceSessions'
import { lastActivityEpoch } from '../pages/chat/sessionOrder'
import type { InstanceView } from '../api/client'

const CONNECTED = { id: 'astro', name: 'astro', status: { state: 'connected' } }
const OFFLINE = { id: 'chick', name: 'chick', status: { state: 'disconnected' } }

/** The caller owns the `['instances']` query, so the hook is handed the list it
 *  already holds. Passing it in is the property under test in
 *  `never opens its own instances query`: a second observer on that key notified
 *  the sidebar twice per cache write, and one such render landing mid-rename
 *  cancelled the edit. */
function renderInstanceSessions(
  enabled: boolean,
  instances: unknown[] = [],
  instancesUnanswered = false,
) {
  return renderHook(
    () => useInstanceSessions(enabled, instances as InstanceView[], instancesUnanswered),
    { wrapper },
  )
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => vi.clearAllMocks())

describe('useInstanceSessions', () => {
  it('issues NO request while the preview flag is off', async () => {
    const { result } = renderInstanceSessions(false, [CONNECTED])
    await waitFor(() => expect(result.current.rows).toHaveLength(0))
    // The gate is the wire, not the render.
    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
  })

  it('never opens its own instances query', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'from the caller’s list', last_turn_ts: '2026-08-31T14:00:00Z' },
    ])
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    // The caller already holds `['instances']`; a second observer on the same key
    // re-rendered the whole sidebar on every cache write.
    expect(listInstancesMock).not.toHaveBeenCalled()
  })

  it('reports the caller’s unanswered instances list as loading', async () => {
    const { result } = renderInstanceSessions(true, [], true)
    await waitFor(() => expect(result.current.loading).toBe(true))
    expect(result.current.rows).toHaveLength(0)
  })

  it('maps a connected instance’s slots onto rows the sessions list can render', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'deploy checklist', agent: 'kirocrew', last_turn_ts: '2026-08-31T14:35:00Z', last_ts: '2026-08-31T14:36:00Z', created: '2026-08-20T09:00:00Z', running: true },
    ])
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const row = result.current.rows[0]
    expect(row.key).toBe('chat-1')
    expect(row.title).toBe('deploy checklist')
    expect(row.peer_id).toBe('astro')
    expect(row.peer_name).toBe('astro')
    // NOT `instance_id`: that field means the opposite direction of travel (a
    // local slot whose turns run on a peer), and stamping it here would both
    // collide with ~two dozen ChatSidebar predicates and clobber a peer slot's
    // own third-machine binding.
    expect(row).not.toHaveProperty('instance_id')
    expect(row).not.toHaveProperty('instance_name')
    expect(row.running).toBe(true)
    // The ISO ladder is forwarded RAW, with nothing derived from it: ranking, the
    // date-segment header and the row label all go through `slotActivityTs`, so a
    // derived epoch field would only be a second copy of the same ladder.
    expect(row.last_turn_ts).toBe('2026-08-31T14:35:00Z')
    expect(row.last_ts).toBe('2026-08-31T14:36:00Z')
    expect(row.created).toBe('2026-08-20T09:00:00Z')
    expect(row).not.toHaveProperty('modified')
    // last_turn_ts wins the ladder, not last_ts (which advances on tool calls) —
    // asserted through the function the list actually ranks on.
    expect(lastActivityEpoch(row)).toBe(Date.parse('2026-08-31T14:35:00Z') / 1000)
    expect(result.current.failed).toEqual([])
  })

  it('preserves row identity across unrelated rerenders after query data settles', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'stable row', last_turn_ts: '2026-08-31T14:00:00Z' },
    ])
    // One frozen array identity, exactly as the sidebar's memoized `instancesList`
    // supplies it — a fresh array each render would legitimately rebuild `rows`.
    const instances = [CONNECTED] as unknown as InstanceView[]
    const { result, rerender } = renderHook(
      ({ tick }: { tick: number }) => {
        void tick
        return useInstanceSessions(true, instances)
      },
      { wrapper, initialProps: { tick: 0 } },
    )

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const firstRows = result.current.rows
    rerender({ tick: 1 })
    expect(result.current.rows).toBe(firstRows)
  })

  it('never queries a disconnected instance', async () => {
    const { result } = renderInstanceSessions(true, [OFFLINE])

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
    expect(result.current.rows).toHaveLength(0)
  })

  it('contains one unreachable instance without losing the others', async () => {
    instanceChatSlotsMock.mockImplementation((id: string) =>
      id === 'baymax'
        ? Promise.reject(new Error('proxy_peer_not_connected'))
        : Promise.resolve([{ key: 'chat-1', title: 'still here', last_turn_ts: '2026-08-31T14:00:00Z' }]),
    )
    const { result } = renderInstanceSessions(true, [
      CONNECTED, { id: 'baymax', name: 'baymax', status: { state: 'connected' } },
    ])

    await waitFor(() => expect(result.current.failed).toEqual(['baymax']))
    // The healthy instance's row survives the other's failure.
    expect(result.current.rows.map(r => r.title)).toEqual(['still here'])
    // The failing read's OWN text rides along with the name. `failed` says WHICH
    // crew went quiet; `failure` says why, and it is the string `ErrorNotice`
    // matches against the error journal to recover the endpoint, HTTP status and
    // backend `code`. Dropping it flattens a fixable `proxy_peer_not_connected`
    // into an unactionable "unavailable".
    expect(result.current.failure).toBe('proxy_peer_not_connected')
  })

  it('keeps the FIRST failure message, matching failed[0]', async () => {
    // Every peer read goes through the same hub route, so a second failure is
    // nearly always a restatement of the first — and a banner that grows a line
    // per crew buries the one line that matters. `failure` therefore pairs with
    // `failed[0]` rather than concatenating, and the pairing has to hold under
    // out-of-order settlement: the SLOWER crew here is the one listed first.
    instanceChatSlotsMock.mockImplementation((id: string) =>
      id === 'astro'
        ? new Promise((_, reject) =>
            setTimeout(() => reject(new Error('astro said no')), 20))
        : Promise.reject(new Error('baymax said no')),
    )
    const { result } = renderInstanceSessions(true, [
      CONNECTED, { id: 'baymax', name: 'baymax', status: { state: 'connected' } },
    ])

    await waitFor(() => expect(result.current.failed).toEqual(['astro', 'baymax']))
    expect(result.current.failure).toBe('astro said no')
  })

  it('names the crew but no message when a rejection carries no Error', async () => {
    // A rejection can carry any value. A non-`Error` reaching the banner as
    // `[object Object]` is worse than saying only which crew went quiet, so the
    // message is dropped and the name is kept.
    instanceChatSlotsMock.mockImplementation(() => Promise.reject({ nope: true }))
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.failed).toEqual(['astro']))
    expect(result.current.failure).toBeUndefined()
  })

  it('drops malformed slots rather than emitting keyless rows', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'ok' },
      { title: 'no key at all' },
      null,
    ])
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    expect(result.current.rows[0].key).toBe('chat-1')
  })

  it('drops non-string peer fields instead of letting React render an object', async () => {
    // A peer is a different machine on a possibly different version, so
    // `PeerSlot`'s types are unverified at runtime. An object reaching `title`
    // renders as a React child and throws, taking the sidebar down — so a
    // malformed field must arrive as absent, not as an object.
    instanceChatSlotsMock.mockResolvedValue([
      {
        key: 'chat-1',
        title: {} as unknown as string,
        agent: 42 as unknown as string,
        created: ['nope'] as unknown as string,
        pending_approval: {} as unknown as boolean,
        last_turn_ts: '2026-08-31T14:00:00Z',
      },
    ])
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const row = result.current.rows[0]
    expect(row.title).toBeUndefined()
    expect(row.agent).toBeUndefined()
    expect(row.created).toBeUndefined()
    // A truthy non-boolean must not raise a badge the peer never claimed.
    expect(row.pending_approval).toBe(false)
    // The one well-formed field still lands.
    expect(row.last_turn_ts).toBe('2026-08-31T14:00:00Z')
  })

  it('falls through a malformed ladder rung to the next valid one', async () => {
    // `slotActivityTs` picks a rung with `||`, so a truthy non-string would WIN
    // that chain and then parse to NaN, discarding the good `last_ts` behind it —
    // and a NaN reaching the comparator makes every comparison false, leaving the
    // whole merged list arbitrarily ordered rather than misplacing one row. The
    // per-rung `str()` guard is what prevents that, so this asserts through
    // `lastActivityEpoch`, the function the list ranks on, rather than through an
    // intermediate field.
    instanceChatSlotsMock.mockResolvedValue([
      {
        key: 'chat-1',
        last_turn_ts: {} as unknown as string,
        last_ts: '2026-08-31T14:36:00Z',
      },
    ])
    const { result } = renderInstanceSessions(true, [CONNECTED])

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const row = result.current.rows[0]
    expect(row.last_turn_ts).toBeUndefined()
    expect(lastActivityEpoch(row)).toBe(new Date('2026-08-31T14:36:00Z').getTime() / 1000)
  })
})
