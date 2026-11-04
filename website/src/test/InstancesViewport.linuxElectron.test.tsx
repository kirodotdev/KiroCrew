/**
 * Frameless Linux desktop shell — host-model relay contract.
 *
 * A frameless Linux window has no OS caption buttons, so the shell injects its
 * own minimize/maximize/close cluster at the window's top-right
 * (#electron-linux-controls). An embedded remote pane is a cross-origin iframe
 * with no preload — `isLinuxFramelessElectron` is false inside it — so the
 * HOST must relay that geometry down: `linuxInset` in `mc-host-model`
 * (mirroring `winInset`), and the host-rendered loading/error strips must
 * inset their own right side. Without it the pane's header puts the
 * notifications bell under the Close button.
 *
 * Deliberately NO vi.mock of ../lib/electron here: `window.kirocrew` is set in
 * a hoisted block BEFORE the module loads, so the real derivation in
 * src/lib/electron.ts is what is under test.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { setWarm } from '../store/instancesSlice'

// Must run before src/lib/electron.ts is imported (module-level consts).
vi.hoisted(() => {
  ;(window as unknown as { kirocrew: { isElectron: boolean; platform: string; linuxFrameless: boolean } }).kirocrew = {
    isElectron: true,
    platform: 'linux',
    linuxFrameless: true,
  }
})

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn(),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
  },
}))
import { api } from '../api/client'

beforeEach(() => {
  vi.mocked(api.listInstances).mockResolvedValue({
    instances: [
      {
        id: 'cd-1',
        name: 'Cloud One',
        ssh_host: 'cd-1-alias',
        remote_port: 7777,
        local_port: 7778,
        ttl: '20h',
        remote_bin: '',
        status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
      },
    ],
    warm_set_cap: 5,
  } as never)
})

describe('InstancesViewport under a frameless Linux shell', () => {
  it('relays linuxInset: true in the mc-host-model payload', async () => {
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'tok' } },
        activeId: 'cd-1',
        mru: ['cd-1'],
        unread: {},
        ready: { 'cd-1': true },
      },
    })
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    const post = vi.fn()
    const frame = document.querySelector('iframe') as HTMLIFrameElement
    Object.defineProperty(frame, 'contentWindow', {
      get: () => ({
        postMessage: post,
        get location() {
          throw new DOMException('blocked', 'SecurityError')
        },
      }),
    })

    act(() => {
      store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok' } }))
    })

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'mc-host-model', linuxInset: true, winInset: false, macInset: false }),
        expect.any(String),
      ),
    )
  })

  it('insets the panel tab bar clear of the Linux caption buttons', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 0,
          ttl: '20h',
          remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    } as never)
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })

    const bar = await screen.findByRole('group', { name: /Remote crews/i })
    expect(bar.getAttribute('style') || '').toContain('padding-right: 112px')
  })
})
