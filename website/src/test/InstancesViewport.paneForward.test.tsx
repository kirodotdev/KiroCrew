/**
 * InstancesViewport in the desktop app's client-only mode: the dashboard is a
 * crew's gateway on another machine, and each pane frames a port that gateway
 * forwarded on ITS machine. The app forwards those ports on demand
 * (`window.remoteCrewPaneAPI`), so a pane's frame must wait for its port before
 * it loads, and a port no pane shows any more must be released.
 *
 * The mocks are copies of InstancesViewport.paneLoad.test.tsx's: a vi.mock
 * factory applies only to the file it is written in.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { removeWarm } from '../store/instancesSlice'
import { api } from '../api/client'
import { mockConnectedCd1 } from './InstancesViewportTestUtils'

vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
}))

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
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'added-1' }),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    embedInstanceToken: vi.fn(),
  },
}))

type Answer = Record<string, 'up' | 'down'> | null

/** A bridge whose every answer the test releases by hand. */
function installBridge() {
  const answers: Array<(answer: Answer) => void> = []
  const forwardPorts = vi.fn((_ports: number[]) => new Promise<Answer>(resolve => { answers.push(resolve) }))
  window.remoteCrewPaneAPI = { forwardPorts }
  return { forwardPorts, answers }
}

const warmCd1 = () =>
  createTestStore({
    instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
  })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  mockConnectedCd1()
})

afterEach(() => {
  vi.useRealTimers()
  delete window.remoteCrewPaneAPI
})

describe('InstancesViewport', () => {
  it('asks the desktop app to forward a pane port and loads the pane only once it is tried', async () => {
    const { forwardPorts, answers } = installBridge()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })

    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    // Mounting the frame before the forward exists would load a refused port.
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()

    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://localhost:7778/?token=tok')
  })

  it('loads a pane whose forward failed, so the watchdog can explain it', async () => {
    const { forwardPorts, answers } = installBridge()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    await act(async () => { answers[answers.length - 1]({ 7778: 'down' }) })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
  })

  it('loads the pane at once when the bridge refuses the window', async () => {
    const forwardPorts = vi.fn(() => Promise.reject(new Error('restricted to the launch port')))
    window.remoteCrewPaneAPI = { forwardPorts }
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(forwardPorts).toHaveBeenCalled()
  })

  it('mounts the pane at once with no desktop bridge, as in a browser', () => {
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://localhost:7778/?token=tok')
  })

  it('does not arm the load watchdog while the forward is still being tried', async () => {
    const { forwardPorts, answers } = installBridge()
    vi.useFakeTimers()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await act(async () => { await Promise.resolve() })
    expect(forwardPorts).toHaveBeenCalledWith([7778])
    // A forward through a slow ProxyCommand can take most of ssh's budget.
    await act(async () => { vi.advanceTimersByTime(15_000) })
    expect(screen.queryByText(/Pane failed to load/i)).toBeNull()

    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })
    // The countdown starts when the frame does.
    await act(async () => { vi.advanceTimersByTime(14_000) })
    expect(screen.queryByText(/Pane failed to load/i)).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1_000) })
    expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
  })

  it('releases a pane port once no pane shows it', async () => {
    const { forwardPorts, answers } = installBridge()
    const store = warmCd1()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })

    act(() => { store.dispatch(removeWarm('cd-1')) })
    await waitFor(() => expect(forwardPorts).toHaveBeenLastCalledWith([]))
  })
})

/** A dashboard token's shape, `base64url(payload).signature`, naming `port`. */
function tokenNaming(port: number): string {
  const b64 = btoa(JSON.stringify({ sub: 'owner', embed_parent_port: String(port) }))
  return `${b64.replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')}.c2ln`
}

function servePageAt(url: string) {
  ;(window as unknown as { happyDOM: { setURL(u: string): void } }).happyDOM.setURL(url)
}

// The managed tunnel forwards this page's 5477 to the gateway's 5476, and the
// gateway mints every token it keeps with 5476. The crew's CSP admits only the
// port a token names, so a pane framed with the gateway's token never loads.
describe('InstancesViewport on a page served on another port than its gateway', () => {
  beforeEach(() => { servePageAt('http://127.0.0.1:5477/') })
  afterEach(() => {
    servePageAt('http://localhost:6776/')
    // clearAllMocks keeps implementations, so put back the module defaults the
    // other cases here rely on, in whatever order they run.
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'tok' })
    vi.mocked(api.refreshInstanceToken).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'tok' })
    vi.mocked(api.embedInstanceToken).mockReset()
  })

  it('frames a warmed pane with a credential naming this page, not the gateway token', async () => {
    const gatewayToken = tokenNaming(5476)
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: gatewayToken })
    vi.mocked(api.embedInstanceToken).mockResolvedValue({ token: 'page-tok', port: 7778, ttl: '30m' })
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {}, ready: {} } })
    renderWithProviders(<InstancesViewport />, { store })

    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(api.embedInstanceToken).toHaveBeenCalledWith('cd-1', 5477)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://127.0.0.1:7778/?token=page-tok')
    expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'page-tok' })
  })

  it('renews a pane through the same credential when it reports its session expired', async () => {
    vi.mocked(api.refreshInstanceToken).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: tokenNaming(5476) })
    vi.mocked(api.embedInstanceToken).mockResolvedValue({ token: 'page-tok-2', port: 7778, ttl: '30m' })
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'page-tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'mc-auth-expired' }, origin: 'http://127.0.0.1:7778' }))
    })

    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'page-tok-2' }))
    expect(api.embedInstanceToken).toHaveBeenCalledWith('cd-1', 5477)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://127.0.0.1:7778/?token=page-tok-2')
  })
})
