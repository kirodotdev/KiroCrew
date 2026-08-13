import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstanceTabBar from '../components/InstanceTabBar'
import EmbeddedHostBridge from '../components/EmbeddedHostBridge'
import type { HostModel } from '../store/instancesSlice'

// Embedded panes never hit the instances API; mock it to a no-op so the import
// is inert and any accidental call is observable.
vi.mock('../api/client', () => ({
  ApiError: class extends Error {},
  api: { listInstances: vi.fn(), connectInstance: vi.fn() },
}))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
import { isEmbeddedPane } from '../lib/embedded'

const model = (over: Partial<HostModel> = {}): HostModel => ({
  tabs: [{ id: 'cd-1', name: 'Cloud One', sshHost: 'cd-1-alias', state: 'connected', unread: 0 }],
  activeId: 'cd-1',
  self: null,
  macInset: false,
  electron: true,
  pinnedCrews: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isEmbeddedPane).mockReturnValue(true)
})
afterEach(() => {
  document.documentElement.classList.remove('embedded-mac-inset')
})

describe('EmbeddedInstanceTabBar (option B)', () => {
  it('renders the relayed switcher and posts a switch request to the parent', async () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: model() },
    })
    renderWithProviders(<InstanceTabBar variant="inline" />, { store })

    // Local + the relayed instance tab both render.
    await userEvent.click(await screen.findByRole('button', { name: /Switch crew/i }))
    expect(screen.getByRole('menuitemradio', { name: /Local/ })).toBeTruthy()
    const cloud = screen.getByRole('menuitemradio', { name: /Cloud One/ })
    expect(cloud).toBeTruthy()

    await userEvent.click(cloud)
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-switch-instance', id: 'cd-1' }),
      '*',
    )

    await userEvent.click(await screen.findByRole('button', { name: /Switch crew/i }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /Local/ }))
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-switch-instance', id: null }),
      '*',
    )
  })

  it('renders nothing when the parent has not relayed a model yet', () => {
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: null },
    })
    const { container } = renderWithProviders(<InstanceTabBar variant="inline" />, { store })
    expect(container.querySelector('[aria-label="Remote crews"]')).toBeNull()
  })

  it('honors the relayed pin set: a pinned crew renders as a chip beside the dropdown', async () => {
    const store = createTestStore({
      instances: {
        warm: {}, activeId: null, mru: [], unread: {},
        // Local is active, so the pinned crew is the one that gets a chip.
        host: model({ activeId: null, pinnedCrews: ['cd-1'] }),
      },
    })
    renderWithProviders(<InstanceTabBar variant="inline" />, { store })
    // The chip row exists and holds the pinned crew. The dropdown stays — it is
    // the trailing chevron that reaches every OTHER crew.
    const row = screen.getByTestId('crew-chip-row')
    expect(row.textContent).toMatch(/Cloud One/)
    expect(screen.getByRole('button', { name: /Switch crew/i })).toBeTruthy()
  })

  it('renders no chip row when the parent relays an empty pin set', () => {
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: model({ activeId: null }) },
    })
    renderWithProviders(<InstanceTabBar variant="inline" />, { store })
    expect(screen.queryByTestId('crew-chip-row')).toBeNull()
  })

  it('relays a pin toggle up to the parent instead of writing its own store', async () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: model({ activeId: null }) },
    })
    renderWithProviders(<InstanceTabBar variant="inline" />, { store })

    // A pane cannot write the parent's preference store from its own iframe
    // realm, so pinning here must travel up as a message rather than persist
    // locally — otherwise the pane would drift from every other bar.
    await userEvent.click(screen.getByRole('button', { name: /Switch crew/i }))
    await userEvent.click(await screen.findByTestId('crew-pin-cd-1'))
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-set-crew-pin', id: 'cd-1' }),
      '*',
    )
  })
})

describe('EmbeddedHostBridge (option B relay)', () => {
  it('pings the parent on mount and ingests a relayed model + toggles the mac inset', async () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore()
    renderWithProviders(<EmbeddedHostBridge />, { store })

    // Announces readiness so the parent (re)sends the model.
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'mc-embedded-ready' }), '*')

    // A message from the parent updates the store + applies the traffic-light inset.
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          source: window.parent,
          data: { type: 'mc-host-model', ...model({ macInset: true, self: { state: 'connected' } }) },
        }),
      )
    })
    await waitFor(() => expect(store.getState().instances.host?.tabs).toHaveLength(1))
    expect(store.getState().instances.host?.macInset).toBe(true)
    expect(document.documentElement.classList.contains('embedded-mac-inset')).toBe(true)
  })

  it('ignores messages that are not from the direct parent', async () => {
    vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore()
    renderWithProviders(<EmbeddedHostBridge />, { store })
    act(() => {
      // source omitted (null) — not window.parent, so it must be rejected.
      window.dispatchEvent(
        new MessageEvent('message', { data: { type: 'mc-host-model', ...model() } }),
      )
    })
    expect(store.getState().instances.host).toBeNull()
  })
})
