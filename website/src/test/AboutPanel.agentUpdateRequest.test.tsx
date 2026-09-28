// An agent's request that this packaged install be updated (issue #503).
//
// The card is context around the click Settings › About ALREADY has: it names
// the version and the requester, and its Install drives the same
// download→install bridge the ordinary update card drives. There is no new IPC
// and no gateway approve — the human click is the approval.
//
// Pinned here:
// - the card names version and requester, and says when the feed's version
//   differs from the one requested
// - Install with nothing staged calls download; with a stage present it calls
//   install; a download the card started finishes with install, once
// - Decline reaches the gateway and never touches the desktop bridge
// - a managed-venv arm does NOT summon the card (that lane prints a command)
// - a shell with no desktop bridge renders nothing
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { AboutPanel, AgentUpdateRequestCard, AGENT_REQUEST_POLL_MS } from '../pages/settings/AboutPanel'
import { api } from '../api/client'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  update_managed_by: 'electron', update_can_apply: false, update_can_arm: false,
} as const

const REQUEST = {
  armed: true, managed_by: 'electron', request_id: 'r1', version: '0.6.0', requested_by: 'chat-42',
  armed_at: Date.now() / 1000 - 30, expires_in: 305,
}

function desktopBridge() {
  return {
    onState: vi.fn(() => () => {}),
    check: vi.fn(async () => ({ ok: true })),
    download: vi.fn(async () => ({ ok: true })),
    install: vi.fn(async () => ({ ok: true })),
    getInfo: vi.fn(async () => ({ version: '0.5.0', channel: 'stable', packaged: true, platform: 'darwin-arm64' })),
  }
}

function renderPanel(client: QueryClient) {
  return render(
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/settings/about']}>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('AgentUpdateRequestCard', () => {
  afterEach(() => cleanup())

  function renderCard(props: Partial<React.ComponentProps<typeof AgentUpdateRequestCard>> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const onInstall = vi.fn()
    const onDeclined = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <AgentUpdateRequestCard request={REQUEST} busy={false} onInstall={onInstall} onDeclined={onDeclined} {...props} />
      </QueryClientProvider>,
    )
    return { onInstall, onDeclined }
  }

  it('names the version and who asked', () => {
    renderCard()
    const body = screen.getByTestId('agent-update-request-body').textContent || ''
    expect(body).toContain('0.6.0')
    expect(body).toContain('chat-42')
    // Humanized, never minutes:seconds — the TTL is a day.
    const when = screen.getByTestId('agent-update-request-countdown').textContent || ''
    expect(when).toMatch(/minute/)
    expect(when).not.toMatch(/\d+:\d\d/)
  })

  it('the Install button names the version it will deliver', () => {
    renderCard({ foundVersion: '0.7.0' })
    expect(screen.getByTestId('agent-update-request-install').textContent).toContain('0.7.0')
    cleanup()
    // No check has run: the plain label, which promises no version.
    renderCard()
    expect(screen.getByTestId('agent-update-request-install').textContent).not.toMatch(/0\.\d/)
  })

  it('a panel-owned failure renders through ErrorNotice', () => {
    renderCard({ panelError: 'the request poll keeps failing' })
    expect(screen.getByTestId('agent-update-request-panel-error').textContent).toContain('keeps failing')
    // And the controls stay live: an error must not strand the user.
    expect(screen.getByTestId('agent-update-request-install')).not.toBeDisabled()
    expect(screen.getByTestId('agent-update-request-decline')).not.toBeDisabled()
  })

  it('says so when the feed offers a different version than was requested', () => {
    renderCard({ foundVersion: '0.7.0' })
    const note = screen.getByTestId('agent-update-request-differs').textContent || ''
    expect(note).toContain('0.6.0')
    expect(note).toContain('0.7.0')
  })

  it('does not warn when the feed matches, or when nothing was named', () => {
    renderCard({ foundVersion: '0.6.0' })
    expect(screen.queryByTestId('agent-update-request-differs')).toBeNull()
    cleanup()
    renderCard({ request: { ...REQUEST, version: '' }, foundVersion: '0.7.0' })
    expect(screen.queryByTestId('agent-update-request-differs')).toBeNull()
  })

  it('Install is the caller\'s click; Decline goes to the gateway', async () => {
    const spy = vi.spyOn(api, 'dismissUpdateArm').mockResolvedValue({ ok: true, armed: false, dismissed: true })
    const { onInstall, onDeclined } = renderCard()
    fireEvent.click(screen.getByTestId('agent-update-request-install'))
    expect(onInstall).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('agent-update-request-decline'))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // Bound to the request the card rendered, so a stale click cannot remove a
    // newer request the user has not seen.
    expect(spy).toHaveBeenCalledWith('r1')
    await waitFor(() => expect(onDeclined).toHaveBeenCalledTimes(1))
    spy.mockRestore()
  })

  it('a failed decline is reported and leaves both controls live', async () => {
    const spy = vi.spyOn(api, 'dismissUpdateArm').mockRejectedValue(new Error('gone'))
    renderCard()
    fireEvent.click(screen.getByTestId('agent-update-request-decline'))
    await screen.findByTestId('agent-update-request-error')
    expect(screen.getByTestId('agent-update-request-install')).not.toBeDisabled()
    spy.mockRestore()
  })
})

describe('AboutPanel with a pending agent request', () => {
  afterEach(() => {
    cleanup()
    delete (window as { updateAPI?: unknown }).updateAPI
    vi.restoreAllMocks()
  })

  function setup(status: object, updateState: object | null = null) {
    const bridge = desktopBridge()
    ;(window as { updateAPI?: unknown }).updateAPI = bridge
    store.dispatch(sseStatus(BLANK_STATUS as never))
    vi.spyOn(api, 'armStatus').mockResolvedValue(status as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    if (updateState) client.setQueryData(['update-state'], updateState)
    renderPanel(client)
    return { bridge, client }
  }

  it('shows the card for a packaged-lane request', async () => {
    setup(REQUEST)
    await screen.findByTestId('agent-update-request')
  })

  it('does not show the card for a managed-venv arm', async () => {
    // That lane is approved with a host command; an in-app Install here would
    // drive the wrong updater.
    setup({ ...REQUEST, managed_by: 'kirocrew', approve_command: 'kirocrew update approve' })
    await waitFor(() => expect(api.armStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-update-request')).toBeNull()
  })

  it('does not show the card when nothing is armed', async () => {
    setup({ armed: false })
    await waitFor(() => expect(api.armStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-update-request')).toBeNull()
  })

  it('Install with nothing staged starts the download through the existing bridge', async () => {
    const { bridge } = setup(REQUEST)
    fireEvent.click(await screen.findByTestId('agent-update-request-install'))
    await waitFor(() => expect(bridge.download).toHaveBeenCalledTimes(1))
    expect(bridge.install).not.toHaveBeenCalled()
  })

  it('Install with a stage present installs through the existing bridge', async () => {
    const { bridge } = setup(REQUEST, { state: 'downloaded', version: '0.6.0' })
    fireEvent.click(await screen.findByTestId('agent-update-request-install'))
    await waitFor(() => expect(bridge.install).toHaveBeenCalledTimes(1))
    expect(bridge.download).not.toHaveBeenCalled()
  })

  it('Install retires the request before dispatching the install', async () => {
    // The install quits the app. A request left on disk would put the card
    // straight back up on relaunch, asking for a version already running.
    const spy = vi.spyOn(api, 'dismissUpdateArm').mockResolvedValue({ ok: true, armed: false, dismissed: true })
    const { bridge } = setup(REQUEST, { state: 'downloaded', version: '0.6.0' })
    fireEvent.click(await screen.findByTestId('agent-update-request-install'))
    await waitFor(() => expect(bridge.install).toHaveBeenCalledTimes(1))
    expect(spy).toHaveBeenCalledWith('r1')
  })

  it('a refused download releases the click and shows the failure', async () => {
    const bridgeApi = desktopBridge()
    bridgeApi.download = vi.fn(async () => { throw new Error('ipc gone') })
    ;(window as { updateAPI?: unknown }).updateAPI = bridgeApi
    store.dispatch(sseStatus(BLANK_STATUS as never))
    vi.spyOn(api, 'armStatus').mockResolvedValue(REQUEST as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    renderPanel(client)
    fireEvent.click(await screen.findByTestId('agent-update-request-install'))
    await screen.findByTestId('agent-update-request-panel-error')
    // Released: both actions are usable again, not disabled forever.
    await waitFor(() => expect(screen.getByTestId('agent-update-request-install')).not.toBeDisabled())
    expect(screen.getByTestId('agent-update-request-decline')).not.toBeDisabled()
  })

  it('a poll that keeps failing is surfaced, and the card stays up', async () => {
    const bridgeApi = desktopBridge()
    ;(window as { updateAPI?: unknown }).updateAPI = bridgeApi
    store.dispatch(sseStatus(BLANK_STATUS as never))
    let calls = 0
    vi.spyOn(api, 'armStatus').mockImplementation(async () => {
      calls += 1
      if (calls === 1) return REQUEST as never
      throw new Error('gateway away')
    })
    // A tiny poll interval stands in for the 5 s one so three consecutive
    // failures land inside the test's budget; the threshold under test is the
    // same one production uses.
    const saved = AGENT_REQUEST_POLL_MS.value
    AGENT_REQUEST_POLL_MS.value = 20
    try {
      // React Query resets `failureCount` at the START of each fetch, so it only
      // reaches the threshold through RETRIES within one poll — which is what
      // production has (the default retry is 3). Keep retries on here, with no
      // delay, or the threshold can never be crossed.
      const client = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 1 } } })
      renderPanel(client)
      await screen.findByTestId('agent-update-request')
      await screen.findByTestId('agent-update-request-panel-error', {}, { timeout: 4000 })
      // React Query keeps the last good data through errors, so the card is
      // still up with its error line — not blanked.
      expect(screen.queryByTestId('agent-update-request')).not.toBeNull()
      expect(calls).toBeGreaterThanOrEqual(4)
    } finally {
      AGENT_REQUEST_POLL_MS.value = saved
    }
  })

  it('the ordinary card yields its Install control while a request is live', async () => {
    setup(REQUEST, { state: 'found', version: '0.7.0' })
    await screen.findByTestId('agent-update-request')
    const ordinary = screen.getByTestId('update-card')
    // One CTA for one update: the request card's, which names the version.
    expect(ordinary.querySelector('button')).toBeNull()
    expect(screen.getByTestId('agent-update-request-install').textContent).toContain('0.7.0')
  })

  it('a download the card started finishes with install, once', async () => {
    vi.spyOn(api, 'dismissUpdateArm').mockResolvedValue({ ok: true, armed: false, dismissed: true })
    const { bridge, client } = setup(REQUEST)
    fireEvent.click(await screen.findByTestId('agent-update-request-install'))
    await waitFor(() => expect(bridge.download).toHaveBeenCalledTimes(1))
    // The updater pushes `downloaded`; the card's click completes.
    client.setQueryData(['update-state'], { state: 'downloaded', version: '0.6.0' })
    await waitFor(() => expect(bridge.install).toHaveBeenCalledTimes(1))
    // A second `downloaded` push (a later, unrelated download) must not ride
    // the same click.
    client.setQueryData(['update-state'], { state: 'downloaded', version: '0.7.0' })
    await new Promise(r => setTimeout(r, 50))
    expect(bridge.install).toHaveBeenCalledTimes(1)
  })
})
