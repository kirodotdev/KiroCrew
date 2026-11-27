/**
 * The warm set: the `warm_set_cap` most recently opened remote Crews, SSH and
 * relay together. SSH crews outside it are not auto-connected, relay Crews in it
 * get a hidden pane through the same open a click makes, a relay Crew never
 * opened gets none, and a Crew that falls out of it loses its warm pane.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { api, ApiError, type InstanceView, type RelayCrewEntry } from '../api/client'
import { noteCrewOpened, rankWarmCrews, recentCrews } from '../lib/crewWarmSet'
import { closeRelayCrews, openRelayCrewTab } from '../hooks/useRelayViewerCrews'
import { useCrewWarmKeeper } from '../hooks/useCrewWarmKeeper'
import { AUTO_CONNECT_KEY, useAutoConnectInstances } from '../hooks/useAutoConnectInstances'
import { relayPaneClosed, relayPaneOpened, setActiveId, setWarm } from '../store/instancesSlice'
import { createTestStore, renderHookWithProviders } from './helpers'

const ssh = (id: string): InstanceView => ({
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
  was_connected: false,
  status: { state: 'disconnected' } as InstanceView['status'],
})

const relay = (endpointId: string): RelayCrewEntry => ({
  endpointId,
  name: endpointId.toUpperCase(),
  crewType: 'PERSONAL',
  presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED',
  crewUrl: `https://${endpointId}.relay.example`,
})

function installBridge() {
  const bridge = {
    openPane: vi.fn(async () => ({ ok: true })),
    prewarmPane: vi.fn(async () => ({ ok: true })),
    closeAll: vi.fn(async () => undefined),
    setPaneBounds: vi.fn(async () => undefined),
    setPaneOverlay: vi.fn(async () => undefined),
    setPaneInactive: vi.fn(async () => undefined),
    closePane: vi.fn(async () => undefined),
    onPaneEvent: vi.fn(() => () => {}),
  }
  ;(window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI = bridge
  return bridge
}

function serve(instances: InstanceView[], crews: RelayCrewEntry[], cap = 5) {
  vi.spyOn(api, 'listInstances').mockResolvedValue({ active: true, instances, warm_set_cap: cap, sso: { state: 'ok' } } as never)
  vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews })
  // A refused connect keeps the store untouched: only who was asked matters here.
  return vi.spyOn(api, 'connectInstance').mockRejectedValue(new Error('unreachable'))
}

const opened = (...keys: string[]) => localStorage.setItem('mc-crew-recent', JSON.stringify(keys))

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  delete (window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI
  vi.restoreAllMocks()
})

describe('rankWarmCrews', () => {
  it('ranks SSH and relay together by recency, up to the cap', () => {
    const recent = ['relay:r1', 'ssh:c', 'relay:r2', 'ssh:a', 'ssh:b', 'relay:r3']
    expect(rankWarmCrews(recent, ['a', 'b', 'c'], ['r1', 'r2', 'r3'], 5))
      .toEqual({ ssh: ['c', 'a', 'b'], relay: ['r1', 'r2'] })
  })

  it('fills free slots with SSH crews never opened, but never with a relay Crew never opened', () => {
    expect(rankWarmCrews(['relay:r1'], ['a', 'b'], ['r1', 'r2'], 5))
      .toEqual({ ssh: ['a', 'b'], relay: ['r1'] })
  })

  it('backfills with never-opened SSH crews only while fewer than the cap were opened', () => {
    const all = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
    // Two opened: three free slots go to never-opened SSH crews in registry order, as on main.
    expect(rankWarmCrews(['ssh:g', 'relay:r1'], all, ['r1', 'r2'], 5))
      .toEqual({ ssh: ['g', 'a', 'b', 'c'], relay: ['r1'] })
    // Five opened: the top five by recency, and no backfill.
    expect(rankWarmCrews(['ssh:g', 'relay:r1', 'ssh:f', 'relay:r2', 'ssh:e', 'ssh:d'], all, ['r1', 'r2'], 5))
      .toEqual({ ssh: ['g', 'f', 'e'], relay: ['r1', 'r2'] })
  })

  it('skips a Crew that can no longer hold a pane', () => {
    expect(rankWarmCrews(['ssh:gone', 'relay:old', 'relay:r1', 'ssh:a'], ['a'], ['r1'], 5))
      .toEqual({ ssh: ['a'], relay: ['r1'] })
  })
})

describe('the opened order', () => {
  it('keeps an open this page could not save, so it is not ranked as never opened', () => {
    opened('ssh:a')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    noteCrewOpened('relay:r1')
    expect(recentCrews()).toEqual(['relay:r1', 'ssh:a'])
  })

  it('starts every write from storage, so a Sign out in another window is not undone here', () => {
    opened('relay:r1', 'ssh:a')
    expect(recentCrews()).toEqual(['relay:r1', 'ssh:a'])
    // Another window forgot the relay Crews.
    opened('ssh:a')
    noteCrewOpened('ssh:b')
    expect(JSON.parse(localStorage.getItem('mc-crew-recent') ?? '[]')).toEqual(['ssh:b', 'ssh:a'])
  })
})

describe('useCrewWarmKeeper with auto-connect', () => {
  it('connects only the SSH crews in the warm set and prewarms only relay Crews opened before', async () => {
    const bridge = installBridge()
    const connect = serve(['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(ssh), [relay('r1'), relay('r2')])
    opened('relay:r1', 'ssh:f', 'ssh:b')
    renderHookWithProviders(() => useAutoConnectInstances())

    // r1, f and b by recency, then a and c (never opened) fill the last two slots.
    await waitFor(() => expect(connect).toHaveBeenCalledTimes(4))
    expect(connect.mock.calls.map(c => c[0]).sort()).toEqual(['a', 'b', 'c', 'f'])
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(1))
    expect(bridge.prewarmPane.mock.calls[0][0]).toBe('r1')
    // Never the click's open, which would raise or re-mint a Crew open elsewhere.
    expect(bridge.openPane).not.toHaveBeenCalled()
  })

  it('prewarms no relay Crew with Auto-connect crews off', async () => {
    localStorage.setItem(AUTO_CONNECT_KEY, '0')
    const bridge = installBridge()
    serve([ssh('a')], [relay('r1')])
    opened('relay:r1')
    const store = createTestStore()
    renderHookWithProviders(() => useAutoConnectInstances(), { store })
    act(() => { store.dispatch(setActiveId('a')) })
    await waitFor(() => expect(recentCrews()[0]).toBe('ssh:a'))
    expect(bridge.prewarmPane).not.toHaveBeenCalled()
  })

  it('forgets the relay Crews opened on Sign out or Switch account, and keeps the SSH ones', async () => {
    installBridge()
    opened('relay:r1', 'ssh:a', 'relay:r2')
    await closeRelayCrews()
    expect(recentCrews()).toEqual(['ssh:a'])
    expect(JSON.parse(localStorage.getItem('mc-crew-recent') ?? '[]')).toEqual(['ssh:a'])
  })

  it('records a Crew brought to the front as the most recent', async () => {
    serve([ssh('a'), ssh('b')], [])
    const store = createTestStore()
    renderHookWithProviders(() => useCrewWarmKeeper(() => true), { store })
    act(() => { store.dispatch(setActiveId('b')) })
    act(() => { store.dispatch(relayPaneOpened({ id: 'r9', name: 'R9' })) })
    expect(recentCrews()).toEqual(['relay:r9', 'ssh:b'])
  })

  it('drops the warm pane of a Crew that falls out of the set, SSH or relay', async () => {
    const bridge = installBridge()
    serve(['a', 'b', 'c'].map(ssh), [relay('r1')], 3)
    opened('ssh:a', 'relay:r1', 'ssh:b', 'ssh:c')
    const store = createTestStore()
    for (const id of ['a', 'b', 'c']) store.dispatch(setWarm({ id, conn: { port: 1, token: 't' } }))
    store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1', gen: -1 }))
    store.dispatch(setActiveId(null))
    renderHookWithProviders(() => useCrewWarmKeeper(() => true), { store })

    // Cap 3: a, r1, b. c is least recent and loses its pane; its tunnel is left alone.
    await waitFor(() => expect(Object.keys(store.getState().instances.warm).sort()).toEqual(['a', 'b']))
    expect(bridge.closePane).not.toHaveBeenCalled()

    // Opening c puts it first: now c, a, r1, and b is out.
    act(() => { store.dispatch(setActiveId('c')) })
    await waitFor(() => expect(Object.keys(store.getState().instances.warm)).not.toContain('b'))

    // Opening b again: b, c, a, so the relay pane r1 is the one to go.
    act(() => { store.dispatch(setActiveId('b')) })
    await waitFor(() => expect(bridge.closePane).toHaveBeenCalledWith('r1'))
  })

  /** Mount the keeper and wait until both lists have answered, so the set is ranked. */
  async function mountKeeper(store: ReturnType<typeof createTestStore>) {
    renderHookWithProviders(() => useCrewWarmKeeper(() => true), { store })
    await waitFor(() => expect(api.relayViewerCrews).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
  }

  it('does not open again a relay Crew the person closed', async () => {
    const bridge = installBridge()
    serve([], [relay('r1')])
    opened('relay:r1')
    const store = createTestStore()
    store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1' }))
    await mountKeeper(store)
    act(() => { store.dispatch(relayPaneClosed('r1')) })
    await act(async () => { await Promise.resolve() })
    expect(bridge.prewarmPane).not.toHaveBeenCalled()
  })

  it('records and keeps a click that joined a prewarm and was answered after another pick', async () => {
    const bridge = installBridge()
    let prewarmed: (v: { ok: boolean }) => void = () => {}
    let clicked: (v: { ok: boolean }) => void = () => {}
    bridge.prewarmPane.mockImplementationOnce(() => new Promise(r => { prewarmed = r }))
    bridge.openPane.mockImplementationOnce(() => new Promise(r => { clicked = r }))
    serve([ssh('x')], [relay('r1')], 1)
    opened('relay:r1')
    const store = createTestStore()
    await mountKeeper(store)
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(1))
    // The person clicks r1 while its prewarm is in flight, then picks SSH crew x.
    let open: Promise<unknown> = Promise.resolve()
    act(() => { open = openRelayCrewTab('r1', 'R1', store.dispatch) })
    act(() => { store.dispatch(setActiveId('x')) })
    await act(async () => { prewarmed({ ok: true }); clicked({ ok: true }); await open })
    expect(recentCrews()[0]).toBe('relay:r1')
    await act(async () => { await Promise.resolve() })
    expect(bridge.closePane).not.toHaveBeenCalledWith('r1')
    expect(store.getState().instances.relayLive).toContain('r1')
  })

  it('keeps a late click on a relay Crew outside the set, with no act around the answer', async () => {
    const bridge = installBridge()
    let clicked: (v: { ok: boolean }) => void = () => {}
    bridge.openPane.mockImplementationOnce(() => new Promise(r => { clicked = r }))
    serve([ssh('x')], [relay('r1'), relay('r2')], 1)
    opened('relay:r1')
    const store = createTestStore()
    await mountKeeper(store)
    let open: Promise<unknown> = Promise.resolve()
    act(() => { open = openRelayCrewTab('r2', 'R2', store.dispatch) })
    act(() => { store.dispatch(setActiveId('x')) })
    // The answer lands as it would in the app: React flushes effects on its own schedule.
    clicked({ ok: true })
    await open
    await waitFor(() => expect(store.getState().instances.relayLive).toContain('r2'))
    expect(recentCrews()[0]).toBe('relay:r2')
    expect(bridge.closePane).not.toHaveBeenCalledWith('r2')
  })

  it('prewarms relay Crews with Remote Crew (SSH) turned off', async () => {
    const bridge = installBridge()
    serve([], [relay('r1')])
    vi.spyOn(api, 'listInstances').mockRejectedValue(new ApiError(403, 'off', JSON.stringify({ error: 'off', code: 'instances_disabled' })))
    opened('relay:r1')
    const store = createTestStore()
    await mountKeeper(store)
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(1))
    expect(bridge.prewarmPane.mock.calls[0][0]).toBe('r1')
  })

  it('tries a failed relay prewarm again after a minute, with no list change', async () => {
    const bridge = installBridge()
    bridge.prewarmPane.mockResolvedValueOnce({ ok: false })
    const T0 = 1_800_000_000_000
    const now = vi.spyOn(Date, 'now').mockReturnValue(T0)
    const timers = vi.spyOn(window, 'setTimeout')
    serve([], [relay('r1')])
    opened('relay:r1')
    const store = createTestStore()
    await mountKeeper(store)
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(timers.mock.calls.some(c => c[1] === 60_000)).toBe(true))
    const retry = timers.mock.calls.find(c => c[1] === 60_000)![0] as () => void
    // Its minute is up: lastTry is that old by now.
    now.mockReturnValue(T0 + 60_000)
    act(() => { retry() })
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(2))
  })

  it('drops a warm entry for an SSH crew outside the set at once, and no Crew in the set loses its pane', async () => {
    const bridge = installBridge()
    serve(['a', 'x'].map(ssh), [relay('r1')], 2)
    opened('relay:r1', 'ssh:a')
    const store = createTestStore()
    store.dispatch(setWarm({ id: 'a', conn: { port: 1, token: 't' } }))
    store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1', gen: -1 }))
    store.dispatch(setActiveId(null))
    await mountKeeper(store)
    // A Connect in Settings, or turning the crew back on, writes x while it is not in front.
    act(() => { store.dispatch(setWarm({ id: 'x', conn: { port: 2, token: 't' } })) })
    await waitFor(() => expect(Object.keys(store.getState().instances.warm)).toEqual(['a']))
    expect(bridge.closePane).not.toHaveBeenCalled()
  })

  it('drops an auto-connect answer for a crew that left the set while it connected', async () => {
    const bridge = installBridge()
    serve([ssh('a')], [relay('r1')], 1)
    let answer: (v: unknown) => void = () => {}
    vi.spyOn(api, 'connectInstance').mockImplementation(() => new Promise(r => { answer = r }) as never)
    opened('ssh:a')
    const store = createTestStore()
    renderHookWithProviders(() => useAutoConnectInstances(), { store })
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('a'))
    // The person opens relay Crew r1 meanwhile: with cap 1, a is out of the set.
    await act(async () => { await openRelayCrewTab('r1', 'R1', store.dispatch) })
    expect(bridge.openPane).toHaveBeenCalled()
    await act(async () => { answer({ state: 'connected', local_port: 7781, token: 't' }) })
    await waitFor(() => expect(store.getState().instances.warm.a).toBeUndefined())
  })

  it('drops an SSH crew kept only while in front once the person leaves it', async () => {
    const bridge = installBridge()
    let clicked: (v: { ok: boolean }) => void = () => {}
    bridge.openPane.mockImplementationOnce(() => new Promise(r => { clicked = r }))
    serve([ssh('s')], [relay('r')], 1)
    opened('ssh:s')
    const store = createTestStore()
    store.dispatch(setWarm({ id: 's', conn: { port: 1, token: 't' } }))
    store.dispatch(setActiveId(null))
    await mountKeeper(store)
    // Start opening relay Crew r, pick s before r answers, then go to Local.
    let open: Promise<unknown> = Promise.resolve()
    act(() => { open = openRelayCrewTab('r', 'R', store.dispatch) })
    act(() => { store.dispatch(setActiveId('s')) })
    await act(async () => { clicked({ ok: true }); await open })
    expect(store.getState().instances.warm.s).toBeDefined()
    act(() => { store.dispatch(setActiveId(null)) })
    await waitFor(() => expect(store.getState().instances.warm.s).toBeUndefined())
  })

  it('stops a queued prewarm once Sign out forgets the relay Crews', async () => {
    const bridge = installBridge()
    let answer: (v: { ok: boolean }) => void = () => {}
    bridge.prewarmPane.mockImplementationOnce(() => new Promise(r => { answer = r }))
    serve([], [relay('r1'), relay('r2')])
    opened('relay:r1', 'relay:r2')
    const store = createTestStore()
    await mountKeeper(store)
    await waitFor(() => expect(bridge.prewarmPane).toHaveBeenCalledTimes(1))
    await act(async () => { await closeRelayCrews() })
    await act(async () => { answer({ ok: true }) })
    expect(bridge.prewarmPane).toHaveBeenCalledTimes(1)
  })

  it('asks the shell again the first time a prewarmed pane is shown, so an error page reloads', async () => {
    const bridge = installBridge()
    serve([], [relay('r1')])
    opened('relay:r1')
    const store = createTestStore()
    await mountKeeper(store)
    await waitFor(() => expect(store.getState().instances.relayLive).toEqual(['r1']))
    expect(bridge.openPane).not.toHaveBeenCalled()
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1' })) })
    await waitFor(() => expect(bridge.openPane).toHaveBeenCalledTimes(1))
    expect(bridge.openPane.mock.calls[0][0]).toBe('r1')
    // A second show is an ordinary tab pick.
    act(() => { store.dispatch(setActiveId(null)) })
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1' })) })
    await act(async () => { await Promise.resolve() })
    expect(bridge.openPane).toHaveBeenCalledTimes(1)
  })
})
