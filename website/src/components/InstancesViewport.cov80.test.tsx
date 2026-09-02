import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import InstancesViewport from './InstancesViewport'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Zzq One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok2' }),
  },
}))
import { api } from '../api/client'

const ORIGIN = 'http://127.0.0.1:7778'

function warmStore(activeId: string | null = 'cd-1') {
  return createTestStore({
    instances: {
      warm: { 'cd-1': { port: 7778, token: 'tok' } },
      activeId,
      mru: ['cd-1'],
      unread: {},
      ready: {},
      host: null,
    },
  })
}

function post(data: unknown, origin = ORIGIN) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data, origin }))
  })
}

describe('InstancesViewport relay listener', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
  })

  it('records an unread count relayed from a warm pane', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 3 })
    expect(store.getState().instances.unread['cd-1']).toBe(3)
  })

  it('rejects a non-finite or negative unread count', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 2 })
    post({ type: 'mc-unread-slots', count: 'zzq' })
    post({ type: 'mc-unread-slots', count: -1 })
    expect(store.getState().instances.unread['cd-1']).toBe(2)
  })

  it('ignores a message from an origin that is not a warm tunnel, and non-object data', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 9 }, 'http://127.0.0.1:9999')
    post({ type: 'mc-unread-slots', count: 9 }, 'https://evil.example')
    post('zzq-string')
    post(null)
    expect(store.getState().instances.unread['cd-1']).toBeUndefined()
  })

  it('re-mints the token when the pane reports an expired session', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(api.refreshInstanceToken).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1'].token).toBe('tok2'))
  })

  it('honours a switch request to Local and to a known instance, but not to an unknown id', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-switch-instance', id: null })
    expect(store.getState().instances.activeId).toBeNull()

    post({ type: 'mc-switch-instance', id: 'cd-1' })
    expect(store.getState().instances.activeId).toBe('cd-1')

    post({ type: 'mc-switch-instance', id: 'zzq-unknown' })
    post({ type: 'mc-switch-instance', id: 42 })
    expect(store.getState().instances.activeId).toBe('cd-1')
  })

  it('marks a pane ready when it announces itself', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    expect(store.getState().instances.ready['cd-1']).toBeFalsy()
    post({ type: 'mc-embedded-ready' })
    expect(store.getState().instances.ready['cd-1']).toBe(true)
  })

  it('sanitizes relayed drag gaps and keeps serving later messages', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({
      type: 'mc-drag-gaps',
      gaps: [
        { x: 10, w: 40 },
        { x: -1, w: 10 },
        { x: 5, w: 0 },
        { x: 'zzq', w: 10 },
        null,
        ...Array.from({ length: 64 }, () => ({ x: 1, w: 1 })),
      ],
    })
    post({ type: 'mc-drag-gaps', gaps: 'not-an-array' })

    // Drag strips are Electron-only, so nothing is painted here — the point is
    // that a hostile payload neither throws nor kills the listener.
    expect(document.querySelectorAll('.host-drag-strip')).toHaveLength(0)
    post({ type: 'mc-unread-slots', count: 5 })
    expect(store.getState().instances.unread['cd-1']).toBe(5)
  })

  it('ignores an unrecognised message type', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'zzq-unknown-type', count: 4 })
    expect(store.getState().instances.unread['cd-1']).toBeUndefined()
    expect(store.getState().instances.ready['cd-1']).toBeFalsy()
  })

  it('rate-limits a burst of expiry reports to a single re-mint', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(api.refreshInstanceToken).toHaveBeenCalledTimes(1))
    post({ type: 'mc-auth-expired' })
    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(store.getState().instances.warm['cd-1'].token).toBe('tok2'))
    expect(api.refreshInstanceToken).toHaveBeenCalledTimes(1)
  })
})
