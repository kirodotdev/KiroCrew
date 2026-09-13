import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { useEffect } from 'react'
const { refreshTerminalState } = vi.hoisted(() => ({ refreshTerminalState: vi.fn(async () => true) }))
vi.mock('../hooks/useBottomTerminal', () => ({ refreshTerminalState }))
import {
  openPopout, bringBack, isPopoutOpen, buildPopoutUrl, popoutWindowName,
  registerPopout, hasFreshBeacon, useTerminalPoppedOut, returnSelfToMain,
  __resetForTests, __setNavigateForTests,
  closeSelfToMain,
} from '../utils/terminalPopout'
import { NAV_CLAIM_MS, type PopoutMsg } from '../utils/popoutController'

class Channel {
  static current: Channel
  onmessage: ((event: { data: PopoutMsg }) => void) | null = null
  posted: PopoutMsg[] = []
  constructor() { Channel.current = this }
  postMessage(message: PopoutMsg) { this.posted.push(message) }
  close() {}
}
const cleanupCallbacks: Array<() => void> = []
const beacon = (scope: string) => `mc-terminal-popout-alive:${scope}`

beforeEach(() => {
  refreshTerminalState.mockReset().mockResolvedValue(true)
  vi.useFakeTimers()
  vi.stubGlobal('BroadcastChannel', Channel)
})
afterEach(() => {
  cleanupCallbacks.splice(0).forEach(fn => fn())
  __resetForTests()
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function Presence({ scope }: { scope: string }) {
  return <div data-testid="presence">{String(useTerminalPoppedOut(scope))}</div>
}

describe('session-scoped terminal windows', () => {
  it('deduplicates A without conflating B or punctuation in session keys', () => {
    const first = { closed: false, close: vi.fn(), focus: vi.fn() }
    const second = { closed: false, close: vi.fn(), focus: vi.fn() }
    const open = vi.spyOn(window, 'open')
      .mockReturnValueOnce(first as unknown as Window)
      .mockReturnValueOnce(second as unknown as Window)
    openPopout('A/B')
    openPopout('A/B')
    openPopout('A_B')
    expect(open).toHaveBeenCalledTimes(2)
    expect(open.mock.calls[0].slice(0, 2)).toEqual([buildPopoutUrl('A/B'), popoutWindowName('A/B')])
    expect(open.mock.calls[0][1]).not.toBe(open.mock.calls[1][1])
    expect(buildPopoutUrl('A/B')).toBe(`${window.location.origin}/popout/terminal?sid=A%2FB`)
    bringBack('A/B')
    expect(first.close).not.toHaveBeenCalled()
    expect(second.close).not.toHaveBeenCalled()
    expect(isPopoutOpen('A/B')).toBe(true)
    Channel.current.onmessage?.({ data: { t: 'close', id: 'terminal-panel:A/B' } })
    expect(isPopoutOpen('A/B')).toBe(false)
    expect(isPopoutOpen('A_B')).toBe(true)
  })

  it('does not detach B when opening B is blocked while A is already open', () => {
    vi.spyOn(window, 'open').mockReturnValueOnce({ closed: false, focus: vi.fn() } as unknown as Window).mockReturnValueOnce(null)
    openPopout('A')
    openPopout('B')
    expect(isPopoutOpen('A')).toBe(true)
    expect(isPopoutOpen('B')).toBe(false)
  })

  it('uses only the matching beacon on reload and after switching sessions', async () => {
    localStorage.setItem(beacon('A'), String(Date.now()))
    localStorage.setItem('mc-terminal-popout-alive', String(Date.now()))
    const { rerender } = render(<Presence scope="A" />)
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
    rerender(<Presence scope="B" />)
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
    localStorage.setItem(beacon('B'), String(Date.now()))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: beacon('B') })) })
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
    await act(async () => { vi.advanceTimersByTime(20_000) })
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
  })

  it('clears A on native close without emitting navigation or clearing B', () => {
    localStorage.setItem(beacon('B'), String(Date.now()))
    cleanupCallbacks.push(registerPopout('A'))
    expect(hasFreshBeacon('A')).toBe(true)
    window.dispatchEvent(new Event('pagehide'))
    expect(hasFreshBeacon('A')).toBe(false)
    expect(hasFreshBeacon('B')).toBe(true)
    expect(Channel.current.posted.some(msg => msg.t === 'nav-request')).toBe(false)
  })

  it('waits for a navigation claim before the main can return A', () => {
    const close = vi.spyOn(window, 'close').mockImplementation(() => {})
    const release = vi.fn()
    cleanupCallbacks.push(registerPopout('A', release))
    returnSelfToMain('A')
    expect(close).not.toHaveBeenCalled()
    expect(release).not.toHaveBeenCalled()
    const request = Channel.current.posted.find(msg => msg.t === 'nav-request')
    expect(request).toMatchObject({ intent: { path: '/chat', slotKey: 'A' } })
    if (request?.t !== 'nav-request') throw new Error('missing navigation request')
    Channel.current.onmessage?.({ data: { t: 'nav-offer', nonce: request.nonce, mainId: 'main' } })
    expect(Channel.current.posted.at(-1)).toMatchObject({ t: 'nav-go', mainId: 'main', intent: { slotKey: 'A' } })
    expect(close).not.toHaveBeenCalled()
    expect(release).not.toHaveBeenCalled()
    Channel.current.onmessage?.({ data: { t: 'bring-back', id: 'terminal-panel:A' } })
    expect(close).toHaveBeenCalledOnce()
    expect(release).toHaveBeenCalledOnce()
    expect(release.mock.invocationCallOrder[0]).toBeLessThan(close.mock.invocationCallOrder[0])
  })

  it('leaves sockets and beacon live when the claimed main cannot reopen the panel', () => {
    const release = vi.fn()
    const close = vi.spyOn(window, 'close').mockImplementation(() => {})
    cleanupCallbacks.push(registerPopout('A', release))
    returnSelfToMain('A')
    const request = Channel.current.posted.find(msg => msg.t === 'nav-request')
    if (request?.t !== 'nav-request') throw new Error('missing navigation request')
    Channel.current.onmessage?.({ data: { t: 'nav-offer', nonce: request.nonce, mainId: 'main' } })
    // The main's failed IDB commit deliberately sends no bring-back message.
    vi.advanceTimersByTime(NAV_CLAIM_MS * 2)
    expect(release).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    expect(hasFreshBeacon('A')).toBe(true)
  })

  it('releases resources before clearing the beacon or announcing close', () => {
    const events: string[] = []
    vi.spyOn(window, 'close').mockImplementation(() => { events.push('window-close') })
    cleanupCallbacks.push(registerPopout('A', () => {
      expect(hasFreshBeacon('A')).toBe(true)
      events.push('release')
    }))
    const post = Channel.current.postMessage.bind(Channel.current)
    vi.spyOn(Channel.current, 'postMessage').mockImplementation(message => {
      if (message.t === 'close') {
        expect(hasFreshBeacon('A')).toBe(false)
        expect(events[0]).toBe('release')
        events.push('announce-close')
      }
      post(message)
    })
    closeSelfToMain()
    expect(events).toEqual(['release', 'window-close', 'announce-close'])
    expect(Channel.current.posted.some(message => message.t === 'nav-request')).toBe(false)
  })

  it('falls back to A in this window when no main claims Return', () => {
    vi.spyOn(window, 'close').mockImplementation(() => {})
    const navigate = vi.fn()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    __setNavigateForTests(navigate)
    cleanupCallbacks.push(registerPopout('A/B'))
    returnSelfToMain('A/B')
    vi.advanceTimersByTime(NAV_CLAIM_MS - 1)
    expect(navigate).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(navigate).toHaveBeenCalledWith('/chat?sid=A%2FB')
    expect(open).not.toHaveBeenCalled()
  })
})

describe('canonical state before terminal return', () => {
  it('holds stale deleted PTYs off until a delayed refresh publishes empty A', async () => {
    let finish!: (ok: boolean) => void
    refreshTerminalState.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    let cachedTabs = ['deleted-pty']
    const connect = vi.fn()
    function Pty({ id }: { id: string }) {
      useEffect(() => { connect(id) }, [id])
      return <div>{id}</div>
    }
    function MainHost() {
      return useTerminalPoppedOut('A') ? <div>detached</div> : <div>returned{cachedTabs.map(id => <Pty key={id} id={id} />)}</div>
    }
    vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    openPopout('A')
    render(<MainHost />)
    act(() => { Channel.current.onmessage?.({ data: { t: 'close', id: 'terminal-panel:A' } }) })
    expect(refreshTerminalState).toHaveBeenCalledWith('A')
    expect(screen.getByText('detached')).toBeVisible()
    expect(connect).not.toHaveBeenCalled()
    await act(async () => { cachedTabs = []; finish(true) })
    expect(screen.getByText('returned')).toBeVisible()
    expect(connect).not.toHaveBeenCalled()
    expect(Channel.current.posted.some(message => message.t === 'nav-request')).toBe(false)
  })

  it('fails closed after a refresh failure, including beyond the beacon TTL', async () => {
    refreshTerminalState.mockResolvedValue(false)
    localStorage.setItem(beacon('A'), String(Date.now()))
    render(<Presence scope="A" />)
    localStorage.removeItem(beacon('A'))
    await act(async () => { window.dispatchEvent(new StorageEvent('storage', { key: beacon('A') })) })
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
    await act(async () => { vi.advanceTimersByTime(30_000) })
    expect(refreshTerminalState).toHaveBeenCalledWith('A')
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
  })

  it('holds A across a main-window switch to B without hijacking B', async () => {
    let finish!: (ok: boolean) => void
    refreshTerminalState.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    localStorage.setItem(beacon('A'), String(Date.now()))
    const { rerender } = render(<Presence scope="A" />)
    rerender(<Presence scope="B" />)
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
    localStorage.removeItem(beacon('A'))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: beacon('A') })) })
    expect(refreshTerminalState).not.toHaveBeenCalled()
    rerender(<Presence scope="A" />)
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
    await act(async () => { finish(true) })
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
  })

  it('does not let an old background refresh release a reopened popup', async () => {
    const completions: Array<(ok: boolean) => void> = []
    refreshTerminalState.mockImplementation(() => new Promise(resolve => { completions.push(resolve) }))
    vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    openPopout('A')
    const { rerender } = render(<Presence scope="A" />)
    const deliver = (message: PopoutMsg) => act(() => { Channel.current.onmessage?.({ data: message }) })
    deliver({ t: 'close', id: 'terminal-panel:A' })
    rerender(<Presence scope="B" />)
    deliver({ t: 'open', id: 'terminal-panel:A' })
    deliver({ t: 'close', id: 'terminal-panel:A' })
    await act(async () => { completions[0](true) })
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
    rerender(<Presence scope="A" />)
    expect(screen.getByTestId('presence')).toHaveTextContent('true')
    expect(completions).toHaveLength(2)
    await act(async () => { completions[1](true) })
    expect(screen.getByTestId('presence')).toHaveTextContent('false')
  })
})
