import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { PinnedCrewRail, setCrewPins } from '../components/InstanceTabBar'
import type { InstanceView, SsoStatus } from '../api/client'
import { api, ApiError } from '../api/client'
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      connectInstance: vi.fn(),
    },
  }
})

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

const conn = (over: Partial<InstanceView> = {}): InstanceView => ({
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
  ...over,
})

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setCrewPins([])
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
})

describe('PinnedCrewRail', () => {
  it('renders nothing when no crew is pinned', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const { container } = renderWithProviders(<PinnedCrewRail />, { store })
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="rail-pinned-crews"]')).toBeNull()
  })

  it('inside a remote pane, tiles pinned crews from the relayed host model and posts a switch up', async () => {
    // A remote pane is a separate cross-origin iframe: its own localStorage is
    // empty, so the local pin store is NOT the source of truth here. The parent
    // relays the shared pin set + crew list through instances.host; the rail must
    // read THAT, or pins vanish the moment you switch into a remote crew.
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: {
        warm: {}, activeId: null, mru: [], unread: {},
        host: {
          tabs: [
            { id: 'cd-1', name: 'Cloud One', sshHost: 'cd-1-alias', state: 'connected', unread: 0 },
            { id: 'cd-2', name: 'Cloud Two', sshHost: 'cd-2-alias', state: 'connected', unread: 0 },
          ],
          // cd-2 is the active crew (shown as the header), so it is NOT tiled;
          // cd-1 is pinned and inactive, so it tiles.
          activeId: 'cd-2',
          self: null,
          macInset: false,
          winInset: false,
          electron: true,
          pinnedCrews: ['cd-1', 'cd-2'],
          stableOrder: false,
        },
      },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail />, { store })

    const tile = await screen.findByTestId('rail-pinned-crew-cd-1')
    expect(tile).toHaveAttribute('aria-label', expect.stringContaining('Cloud One'))
    // The active crew (cd-2) is the header, so it is NOT also tiled in the list.
    expect(screen.queryByTestId('rail-pinned-crew-cd-2')).toBeNull()
    // The local instances poll must NOT run in an embedded pane.
    expect(api.listInstances).not.toHaveBeenCalled()

    await u.click(tile)
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-switch-instance', id: 'cd-1' }),
      '*',
    )
  })

  it('inside a remote pane, tiles pinned Local so you can switch back in one click', async () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: {
        warm: {}, activeId: null, mru: [], unread: {},
        host: {
          tabs: [{ id: 'cd-1', name: 'Cloud One', sshHost: 'cd-1-alias', state: 'connected', unread: 0 }],
          activeId: 'cd-1', // a remote is on screen; Local is reachable via the tile
          self: null, macInset: false, winInset: false, electron: true,
          pinnedCrews: ['__local__', 'cd-1'],
          stableOrder: false,
        },
      },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail />, { store })

    const localTile = await screen.findByTestId('rail-pinned-crew-__local__')
    expect(localTile).toBeInTheDocument()
    await u.click(localTile)
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-switch-instance', id: null }),
      '*',
    )
  })

  it('tiles a pinned remote crew and switches to it on a single click', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    setCrewPins(['cd-1'])
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail />, { store })

    const tile = await screen.findByTestId('rail-pinned-crew-cd-1')
    // The tile names the crew and its host for assistive tech.
    expect(tile).toHaveAttribute('aria-label', expect.stringContaining('Cloud One'))

    await u.click(tile)
    await waitFor(() => expect(store.getState().instances.activeId).toBe('cd-1'))
  })

  it('surfaces a rail-initiated connect failure as an always-on error notice', async () => {
    // A pinned crew that tiles (status connected) but has NO warm iframe, so a
    // click fires connectInstance -> 503. The rail runs its OWN connect
    // mutation, so this error reaches no other surface; it must be shown here
    // (errors-use-error-notice), not swallowed into a silent "Disconnected".
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    vi.mocked(api.connectInstance).mockRejectedValue(new ApiError(503, 'remote crew unavailable'))
    setCrewPins(['cd-1'])
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail />, { store })

    const tile = await screen.findByTestId('rail-pinned-crew-cd-1')
    await u.click(tile)

    const notice = await screen.findByTestId('instance-tab-bar-list-error')
    expect(notice).toHaveTextContent(/remote crew unavailable/i)
  })

  it('excludes the active crew from the tiles so it is not shown in two places', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    // '__local__' is the pin value for the local dashboard.
    setCrewPins(['__local__', 'cd-1'])

    // When Local is active (activeId null), Local is NOT tiled (it is the header);
    // the pinned remote cd-1 still tiles.
    const onLocal = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const r1 = renderWithProviders(<PinnedCrewRail />, { store: onLocal })
    expect(await screen.findByTestId('rail-pinned-crew-cd-1')).toBeInTheDocument()
    expect(screen.queryByTestId('rail-pinned-crew-__local__')).toBeNull()
    r1.unmount()

    // When the remote cd-1 is active, cd-1 is NOT tiled; the pinned Local shows
    // (it is no longer the active one), giving a one-click route back to Local.
    const onRemote = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<PinnedCrewRail />, { store: onRemote })
    expect(await screen.findByTestId('rail-pinned-crew-__local__')).toBeInTheDocument()
    expect(screen.queryByTestId('rail-pinned-crew-cd-1')).toBeNull()
  })

  it('vertical rail shows three tiles collapsed, and all crews in a flyout on hover', async () => {
    const many = [
      conn({ id: 'cd-1', name: 'One', ssh_host: 'h1' }),
      conn({ id: 'cd-2', name: 'Two', ssh_host: 'h2' }),
      conn({ id: 'cd-3', name: 'Three', ssh_host: 'h3' }),
      conn({ id: 'cd-4', name: 'Four', ssh_host: 'h4' }),
    ]
    vi.mocked(api.listInstances).mockResolvedValue(listResp(many))
    setCrewPins(['cd-1', 'cd-2', 'cd-3', 'cd-4'])
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail orientation="vertical" />, { store })

    // Collapsed: the rail shows exactly the first three tiles in place.
    const cluster = await screen.findByTestId('rail-pinned-crews')
    expect(within(cluster).getByTestId('rail-pinned-crew-cd-1')).toBeInTheDocument()
    expect(within(cluster).getByTestId('rail-pinned-crew-cd-2')).toBeInTheDocument()
    expect(within(cluster).getByTestId('rail-pinned-crew-cd-3')).toBeInTheDocument()
    expect(within(cluster).queryByTestId('rail-pinned-crew-cd-4')).toBeNull()
    expect(screen.queryByTestId('rail-pinned-crews-flyout')).toBeNull()

    // Hover opens a flyout containing EVERY pinned crew (escaping the rail clip).
    await u.hover(cluster)
    const flyout = await screen.findByTestId('rail-pinned-crews-flyout')
    for (const id of ['cd-1', 'cd-2', 'cd-3', 'cd-4']) {
      expect(within(flyout).getByTestId(`rail-pinned-crew-${id}`)).toBeInTheDocument()
    }
  })

  it('vertical rail shows no flyout when two or fewer are pinned (all fit in the rail)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([
      conn({ id: 'cd-1', name: 'One', ssh_host: 'h1' }),
      conn({ id: 'cd-2', name: 'Two', ssh_host: 'h2' }),
    ]))
    setCrewPins(['cd-1', 'cd-2'])
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<PinnedCrewRail orientation="vertical" />, { store })

    const cluster = await screen.findByTestId('rail-pinned-crews')
    expect(within(cluster).getByTestId('rail-pinned-crew-cd-2')).toBeInTheDocument()
    // Nothing overflows, so hovering opens no flyout.
    await u.hover(cluster)
    expect(screen.queryByTestId('rail-pinned-crews-flyout')).toBeNull()
  })
})
