/**
 * #22680: a reload keeps the crew the window was showing; a new window, or a
 * crew that is gone, opens on Local. "Reload" here is a fresh mount over a
 * fresh store (activeId = null, as after a real reload) with sessionStorage
 * left as the previous page wrote it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { setActiveId, setWarm } from '../store/instancesSlice'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    listInstances: vi.fn(),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok2' }),
  },
}))
import { api } from '../api/client'

const KEY = 'mc-active-pane'

function crew(id: string) {
  return {
    id,
    name: id,
    ssh_host: `${id}-alias`,
    remote_port: 7777,
    local_port: 7778,
    ttl: '20h',
    remote_bin: '',
    status: { instance_id: id, state: 'connected', local_port: 7778, remote_port: 7777 },
  }
}

/** A just-reloaded window: nothing warm, nothing active. */
function reloadedStore() {
  return createTestStore({
    instances: { warm: {}, activeId: null, mru: [], unread: {}, ready: {}, host: null },
  })
}

async function mountAfterFirstPoll(store: ReturnType<typeof reloadedStore>) {
  renderWithProviders(<InstancesViewport />, { store })
  await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
  // Let the poll resolve and the restore effect run.
  await act(async () => { await Promise.resolve() })
}

describe('InstancesViewport keeps the active crew across a reload (#22680)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(api.listInstances).mockReset()
    vi.mocked(api.connectInstance).mockReset().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
    vi.mocked(api.listInstances).mockResolvedValue({ instances: [crew('cd-1'), crew('cd-2')], warm_set_cap: 5 })
  })

  it('a reload restores the crew the window was showing', async () => {
    sessionStorage.setItem(KEY, 'cd-2')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)

    await waitFor(() => expect(store.getState().instances.activeId).toBe('cd-2'))
    // Same condition as a tab click: a pane with no warm iframe is connected.
    await waitFor(() => expect(store.getState().instances.warm['cd-2']).toBeDefined())
    expect(sessionStorage.getItem(KEY)).toBe('cd-2')
  })

  it('refetches live status after restoring a disconnected crew without waiting for the poll', async () => {
    vi.mocked(api.listInstances)
      .mockResolvedValueOnce({
        instances: [{ ...crew('cd-2'), was_connected: true,
          status: { instance_id: 'cd-2', state: 'disconnected' } }],
        warm_set_cap: 5,
      })
      .mockResolvedValue({ instances: [crew('cd-2')], warm_set_cap: 5 })
    sessionStorage.setItem(KEY, 'cd-2')
    const store = reloadedStore()
    // Keep the first list fresh so mounting the panel's tab bar cannot refetch it.
    renderWithProviders(<InstancesViewport />, { store, queryDefaults: { staleTime: Infinity } })

    await waitFor(() => expect(store.getState().instances.warm['cd-2']).toBeDefined())
    await waitFor(() => expect(api.listInstances).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Loading pane…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('instances-viewport-panel-error')).not.toBeInTheDocument()
    expect(screen.getByTitle('cd-2')).toBeVisible()
    expect(store.getState().instances.activeId).toBe('cd-2')
    expect(api.connectInstance).toHaveBeenCalledExactlyOnceWith('cd-2')
  })

  it('keeps the restored crew active and shows a rejected connect error', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [{ ...crew('cd-2'), enabled: false, was_connected: true,
        status: { instance_id: 'cd-2', state: 'disconnected' } }],
      warm_set_cap: 5,
    })
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('crew is disabled'))
    sessionStorage.setItem(KEY, 'cd-2')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)

    expect(await screen.findByText('crew is disabled')).toBeInTheDocument()
    expect(store.getState().instances.activeId).toBe('cd-2')
    expect(sessionStorage.getItem(KEY)).toBe('cd-2')
    expect(api.connectInstance).toHaveBeenCalledExactlyOnceWith('cd-2')
  })

  it('drops a refused restore error once the user leaves that crew', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [{ ...crew('cd-2'), enabled: false, was_connected: true,
        status: { instance_id: 'cd-2', state: 'disconnected' } }],
      warm_set_cap: 5,
    })
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('crew is disabled'))
    sessionStorage.setItem(KEY, 'cd-2')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)
    expect(await screen.findByText('crew is disabled')).toBeInTheDocument()

    act(() => { store.dispatch(setActiveId(null)) })
    act(() => { store.dispatch(setActiveId('cd-2')) })

    expect(store.getState().instances.activeId).toBe('cd-2')
    // The crew's own disconnected panel is back, without the old refusal.
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('crew is disabled')).not.toBeInTheDocument()
  })

  it('drops a refused restore error once something else warms that crew', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [{ ...crew('cd-2'), enabled: false, was_connected: true,
        status: { instance_id: 'cd-2', state: 'disconnected' } }],
      warm_set_cap: 5,
    })
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('crew is disabled'))
    sessionStorage.setItem(KEY, 'cd-2')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)
    expect(await screen.findByText('crew is disabled')).toBeInTheDocument()

    // A tab click or auto-connect warms the crew through its own connect.
    act(() => { store.dispatch(setWarm({ id: 'cd-2', conn: { port: 7790, token: 'fresh' } })) })

    await waitFor(() => expect(screen.queryByText('crew is disabled')).not.toBeInTheDocument())
    expect(store.getState().instances.activeId).toBe('cd-2')
  })

  it('a crew that is gone falls back to Local and forgets the stale id', async () => {
    sessionStorage.setItem(KEY, 'deleted-crew')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)

    await waitFor(() => expect(sessionStorage.getItem(KEY)).toBeNull())
    expect(store.getState().instances.activeId).toBeNull()
    expect(api.connectInstance).not.toHaveBeenCalledWith('deleted-crew', expect.anything())
  })

  it('a crew still listed but no longer in the switcher falls back to Local', async () => {
    // Disconnected with no sticky connect intent: not a switcher tab.
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [{ ...crew('cd-1'), status: { instance_id: 'cd-1', state: 'disconnected' }, was_connected: false }],
      warm_set_cap: 5,
    })
    sessionStorage.setItem(KEY, 'cd-1')
    const store = reloadedStore()
    await mountAfterFirstPoll(store)

    await waitFor(() => expect(sessionStorage.getItem(KEY)).toBeNull())
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('a new window (empty storage) opens on Local', async () => {
    const store = reloadedStore()
    await mountAfterFirstPoll(store)
    // Auto-warm still pre-mounts the connected crews, hidden.
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toBeDefined())

    expect(store.getState().instances.activeId).toBeNull()
    expect(sessionStorage.getItem(KEY)).toBeNull()
  })

  it('records each switch so the next reload can restore it, and Local clears it', async () => {
    const store = reloadedStore()
    await mountAfterFirstPoll(store)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())

    act(() => { store.dispatch(setActiveId('cd-1')) })
    await waitFor(() => expect(sessionStorage.getItem(KEY)).toBe('cd-1'))
    act(() => { store.dispatch(setActiveId(null)) })
    await waitFor(() => expect(sessionStorage.getItem(KEY)).toBeNull())
  })
})
