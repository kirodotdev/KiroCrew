// Settings > About after an approved in-app update installed the offered version
// beside the running one.
//
// Contract under test:
// - the card reads `update_restart_ready` / `update_restart_ready_version` from the
//   status frame, so a fresh mount (a reload) shows a Restart action, not the
//   Update button for the version that is already installed
// - that Restart action posts /api/restart after its two-step confirm
// - a staged version that is not the offered one leaves the Update offer up
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

/** A managed-venv install with 9.9.9 offered. */
const ARMABLE_STATUS = {
  ...BLANK_STATUS,
  update_available: true,
  update_can_apply: false,
  update_can_arm: true,
  update_latest_version: '9.9.9',
  update_channel: 'insider',
  update_managed_by: 'kirocrew',
  update_command: 'curl -fsSL https://example.invalid/cli.sh | sh',
} as const

function stubFetch(checkAnswer: Record<string, unknown> = {}) {
  const posts: { url: string }[] = []
  const json = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: { method?: string }) => {
    const url = String(input)
    if (init?.method === 'POST') posts.push({ url })
    if (url.includes('/api/update/arm')) return json({ armed: false })
    if (url.includes('/api/restart')) return json({ ok: true, status: 'restarting' })
    if (url.includes('/api/update/check')) return json(checkAnswer)
    return json({})
  }))
  return posts
}

function mountWeb() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('AboutPanel: an installed update waiting for a restart', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('shows Restart instead of Update for the installed version after a reload', async () => {
    const posts = stubFetch()
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      update_restart_ready: true,
      update_restart_ready_version: '9.9.9',
      update_restart_ready_reason: 'staged',
    } as never))
    mountWeb()

    await screen.findByTestId('in-app-update-installed')
    expect(screen.queryByTestId('in-app-update-action')).toBeNull()
    expect(screen.queryByTestId('in-app-update')).toBeNull()
    // The hero badge reads the same status fact as the card.
    expect(screen.getByTestId('hero-update-ready')).toBeTruthy()
    expect(screen.queryByTestId('hero-update-available')).toBeNull()

    // Two-step confirm, as every restart control: the first click posts nothing.
    fireEvent.click(screen.getByTestId('gateway-restart-installed'))
    expect(posts.some(p => p.url.includes('/api/restart'))).toBe(false)
    fireEvent.click(screen.getByTestId('gateway-restart-installed'))
    await waitFor(() => expect(posts.some(p => p.url.includes('/api/restart'))).toBe(true))
    // Restarting is the only action here: nothing was armed again.
    expect(posts.some(p => p.url.includes('/api/update/arm'))).toBe(false)
  })

  it('offers Update for a newer build a Check finds while an older one is staged', async () => {
    // v1 (9.9.9) is staged and the status frame still names it as the latest
    // with no offer up yet; a Check then finds v2 (10.0.0) before the next
    // status push.
    stubFetch({
      check_status: 'succeeded', update_available: true, can_apply: false, can_arm: true,
      latest_version: '10.0.0', latest_version_display: '10.0.0', last_checked_at: 200,
    })
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      update_available: false,
      update_check_status: 'succeeded',
      update_last_checked_at: 100,
      update_restart_ready: true,
      update_restart_ready_version: '9.9.9',
      update_restart_ready_reason: 'staged',
    } as never))
    mountWeb()

    fireEvent.click(await screen.findByTestId('about-check-for-updates'))

    const action = await screen.findByTestId('in-app-update-action')
    expect(action.textContent).toContain('10.0.0')
    expect(screen.queryByTestId('in-app-update-installed')).toBeNull()
    expect(screen.queryByTestId('gateway-restart-installed')).toBeNull()
    expect(screen.getByTestId('hero-update-available')).toBeTruthy()
    expect(screen.queryByTestId('hero-update-ready')).toBeNull()
  })

  it('names the running version beside the installed one', async () => {
    stubFetch()
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      version: '9.9.0rc1',
      version_display: '9.9.0-insider.1',
      update_restart_ready: true,
      update_restart_ready_version: '9.9.9',
      update_restart_ready_reason: 'staged',
    } as never))
    mountWeb()

    const card = await screen.findByTestId('in-app-update-installed')
    expect(card.textContent).toContain('9.9.9')
    expect(card.textContent).toContain('9.9.0-insider.1')
  })

  it('shows Restart instead of Update for an update armed for the next start after a reload', async () => {
    const posts = stubFetch()
    store.dispatch(sseStatus({ ...ARMABLE_STATUS, update_armed_version: '9.9.9' } as never))
    mountWeb()

    await screen.findByTestId('gateway-restart-armed')
    expect(screen.queryByTestId('in-app-update-action')).toBeNull()
    expect(screen.getByTestId('about-update-armed-restart')).toBeTruthy()
    expect(screen.queryByTestId('about-update-armed-note')).toBeNull()
    expect(screen.getByTestId('about-update-cancel-armed')).toBeTruthy()
    // The hero badge names the armed state as the header pill does.
    expect(screen.getByTestId('hero-update-armed')).toBeTruthy()
    expect(screen.queryByTestId('hero-update-ready')).toBeNull()

    fireEvent.click(screen.getByTestId('gateway-restart-armed'))
    fireEvent.click(screen.getByTestId('gateway-restart-armed'))
    await waitFor(() => expect(posts.some(p => p.url.includes('/api/restart'))).toBe(true))
    expect(posts.some(p => p.url.includes('/api/update/arm'))).toBe(false)
  })

  it('keeps the generic armed note and Cancel when another version is offered', async () => {
    stubFetch()
    store.dispatch(sseStatus({ ...ARMABLE_STATUS, update_armed_version: '9.9.8' } as never))
    mountWeb()

    await screen.findByTestId('in-app-update-action')
    expect(screen.getByTestId('about-update-armed-note')).toBeTruthy()
    expect(screen.queryByTestId('about-update-armed-restart')).toBeNull()
    expect(screen.getByTestId('about-update-cancel-armed')).toBeTruthy()
    expect(screen.queryByTestId('gateway-restart-armed')).toBeNull()
  })

  it('keeps the Update offer when the installed version is not the offered one', async () => {
    stubFetch()
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      update_restart_ready: true,
      update_restart_ready_version: '9.9.8',
      update_restart_ready_reason: 'staged',
    } as never))
    mountWeb()

    await screen.findByTestId('in-app-update-action')
    expect(screen.queryByTestId('in-app-update-installed')).toBeNull()
    expect(screen.queryByTestId('gateway-restart-installed')).toBeNull()
  })
})
