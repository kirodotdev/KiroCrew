/**
 * Auto-connect backs off a crew whose connect keeps failing.
 *
 * The viewport polls the ['instances'] list every minute, and every poll fans
 * auto-connect out at each crew in the warm set that is not live. A crew whose
 * host refuses the SSH sign-in never comes up, so without a backoff each poll
 * spends another SSH session and token mint on it, all day. These tests drive
 * the polls as focus refreshes on a pinned clock and count the connects.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type InstanceView } from '../api/client'
import { useAutoConnectInstances } from '../hooks/useAutoConnectInstances'
import { connectInstanceInto } from '../lib/connectInstance'
import {
  CONNECT_BACKOFF_BASE_MS,
  CONNECT_BACKOFF_MAX_MS,
  __resetConnectBackoffForTests,
  autoConnectDue,
  connectBackoffDelay,
  noteAutoConnectFailed,
} from '../lib/connectBackoff'
import { createTestStore, renderHookWithProviders } from './helpers'

/** One poll of the viewport's list; the backoff's base is exactly one poll. */
const POLL_MS = CONNECT_BACKOFF_BASE_MS
const T0 = Date.UTC(2026, 10, 26, 10, 0, 0)

/** A crew whose tunnel reads connected while its token cannot be re-minted. */
const crew = (id: string): InstanceView => ({
  id,
  name: id,
  ssh_host: id,
  remote_port: 0,
  local_port: 0,
  ttl: '',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  was_connected: true,
  status: { state: 'connected' } as InstanceView['status'],
})

const refused = () =>
  new ApiError(502, "the crew's host refused the SSH key or certificate", JSON.stringify({ code: 'ssh_auth_refused' }))

/** The hook under test plus a probe that records each list answer it saw. The
 *  probe's effect is declared after the hook's, so once it has run for an
 *  answer, the hook's fan-out for that same answer has run too. */
function mount() {
  const seen: number[] = []
  const store = createTestStore()
  const view = renderHookWithProviders(() => {
    useAutoConnectInstances()
    const qc = useQueryClient()
    const q = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
    useEffect(() => { seen.push(q.dataUpdatedAt) }, [q.dataUpdatedAt])
    return qc
  }, { store })
  return { ...view, seen }
}

let list: ReturnType<typeof vi.spyOn>
let connect: ReturnType<typeof vi.spyOn>
/** The pinned time of each connect the fan-out made. */
let calledAt: number[]

beforeEach(() => {
  localStorage.clear()
  __resetConnectBackoffForTests()
  vi.useFakeTimers({ toFake: ['Date'], now: T0 })
  calledAt = []
  list = vi.spyOn(api, 'listInstances').mockImplementation(async () => (
    { active: true, instances: [crew('cdd2')], warm_set_cap: 5, sso: { state: 'ok' } } as never
  ))
  vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
  connect = vi.spyOn(api, 'connectInstance').mockImplementation(async () => {
    calledAt.push(Date.now())
    throw refused()
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/** Wait until the fan-out for the answer at `t` has run, and, when it made a
 *  connect, until that connect failed and the fan-out finished: a finished
 *  fan-out refreshes the list once more, after it has recorded the failure. */
async function settle(view: ReturnType<typeof mount>, t: number, before: { lists: number; connects: number }) {
  await waitFor(() => expect(view.seen).toContain(t))
  const refreshes = connect.mock.calls.length > before.connects ? 2 : 1
  await waitFor(() => expect(list.mock.calls.length).toBeGreaterThanOrEqual(before.lists + refreshes))
  await waitFor(() => expect(view.result.current.isFetching({ queryKey: ['instances'] })).toBe(0))
}

/** One poll at `t`: the list refetches, and the fan-out runs on its answer. */
async function pollAt(view: ReturnType<typeof mount>, t: number) {
  vi.setSystemTime(t)
  const before = { lists: list.mock.calls.length, connects: connect.mock.calls.length }
  act(() => { window.dispatchEvent(new Event('focus')) })
  await settle(view, t, before)
}

/** The polls (as offsets from the first) at which a crew failing every time is
 *  tried, over `polls` polls after the first. */
function backoffSchedule(polls: number): number[] {
  const tried: number[] = []
  let due = 0
  for (let at = 0; at <= polls * POLL_MS; at += POLL_MS) {
    if (at < due) continue
    tried.push(at)
    due = at + connectBackoffDelay(tried.length)
  }
  return tried
}

describe('connectBackoffDelay', () => {
  it('waits one poll after the first failure, doubles after each, and stops at the cap', () => {
    expect(connectBackoffDelay(1)).toBe(CONNECT_BACKOFF_BASE_MS)
    expect(connectBackoffDelay(2)).toBe(2 * CONNECT_BACKOFF_BASE_MS)
    expect(connectBackoffDelay(3)).toBe(4 * CONNECT_BACKOFF_BASE_MS)
    expect(connectBackoffDelay(1000)).toBe(CONNECT_BACKOFF_MAX_MS)
  })
})

describe('auto-connect backoff', () => {
  it('does not retry a crew that keeps failing on every poll', async () => {
    const view = mount()
    await settle(view, T0, { lists: 0, connects: 0 })
    expect(connect).toHaveBeenCalledTimes(1)
    for (let poll = 1; poll <= 30; poll++) await pollAt(view, T0 + poll * POLL_MS)
    // Retrying on every poll would make 31 connects in these 31 polls.
    const expected = backoffSchedule(30)
    expect(expected.length).toBeLessThan(8)
    expect(calledAt.map(t => t - T0)).toEqual(expected)
  })

  it('tries the crew on the next poll once a tab click has asked for it', async () => {
    const view = mount()
    await settle(view, T0, { lists: 0, connects: 0 })
    await pollAt(view, T0 + POLL_MS)
    // Two failures: the next auto-connect is not due for two more polls.
    expect(autoConnectDue('cdd2', T0 + 2 * POLL_MS)).toBe(false)
    vi.setSystemTime(T0 + 2 * POLL_MS)
    await expect(connectInstanceInto(view.store.dispatch, 'cdd2', 'select')).rejects.toThrow()
    // The click failed too, but the person may have just renewed the key.
    expect(autoConnectDue('cdd2', T0 + 2 * POLL_MS)).toBe(true)
  })

  it('forgets the failures once a connect warms the crew', async () => {
    noteAutoConnectFailed('cdd2', T0)
    noteAutoConnectFailed('cdd2', T0 + POLL_MS)
    connect.mockResolvedValueOnce({ instance_id: 'cdd2', state: 'connected', local_port: 7781, token: 't' })
    const store = createTestStore()
    await connectInstanceInto(store.dispatch, 'cdd2', 'auto-warm', { onlyIfConnected: true })
    expect(store.getState().instances.warm.cdd2).toBeDefined()
    expect(autoConnectDue('cdd2', T0 + POLL_MS)).toBe(true)
  })
})
