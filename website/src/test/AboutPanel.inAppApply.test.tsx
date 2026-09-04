// In-app wheel update (arm + host-local approve) in Settings > About.
//
// Contract under test:
// - the Update button renders ONLY when the backend probed the managed-venv
//   shape (`update_can_arm`); managed_by alone must not summon it
// - clicking it POSTs /api/update/arm and swaps to the armed state showing
//   the approve command and countdown — and NEVER any nonce
// - an arm refusal surfaces as an inline error, not a dead button
// - the manual installer command stays reachable behind the details fold
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus, setUpdateProgress } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

/** Status shape for a managed-venv install with a pending update. */
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

function stubFetch(overrides: Record<string, unknown> = {}) {
  const json = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown, init?: { method?: string }) => {
    const url = String(input)
    if (url.includes('/api/update/arm') && init?.method === 'POST') {
      if (overrides.armError) {
        return {
          ok: false, status: 409,
          json: async () => ({ error: 'no update-available verdict', code: 'arm_no_verdict' }),
          text: async () => '',
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      }
      return json({
        ok: true, armed: true, request_id: 'r1', version: '9.9.9',
        expires_in: 600, approve_command: 'kirocrew update approve',
      })
    }
    if (url.includes('/api/update/arm')) {
      return json({ armed: true, request_id: 'r1', version: '9.9.9', expires_in: 590, approve_command: 'kirocrew update approve' })
    }
    if (url.includes('/api/update/check')) return json({})
    if (url.includes('/api/changelog')) return json({ content: '' })
    return json({})
  })
  vi.stubGlobal('fetch', spy)
  return spy
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

describe('AboutPanel in-app update (arm + approve)', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('renders the Update button for an armable install and arms on click', async () => {
    const spy = stubFetch()
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    const flow = await screen.findByTestId('in-app-update')
    expect(flow).toBeTruthy()
    const btn = screen.getByRole('button', { name: /update to v9\.9\.9/i })
    fireEvent.click(btn)

    const armed = await screen.findByTestId('in-app-update-armed')
    expect(armed).toBeTruthy()
    expect(screen.getByTestId('approve-command').textContent).toContain('kirocrew update approve')
    // The countdown renders from expires_in.
    expect(screen.getByTestId('arm-countdown').textContent).toMatch(/10:00|9:5\d/)
    // The arm POST went out; nothing in the DOM carries a nonce-shaped secret.
    const armPost = spy.mock.calls.find(
      c => String(c[0]).includes('/api/update/arm') && (c[1] as { method?: string })?.method === 'POST',
    )
    expect(armPost).toBeTruthy()
    expect(document.body.innerHTML).not.toMatch(/[0-9a-f]{64}/)
  })

  it('arms the exact promoted stamp on stable, never a folded display version', async () => {
    // A promoted stable candidate's own update_latest_version still carries
    // its insider/rc stamp (promotion never re-stamps the bytes). The In-App
    // Update flow's version prop, and the arm POST it sends, must be that raw
    // stamp -- the shadow-venv apply step later compares it byte-for-byte
    // against the installed build's own never-folded __version__. Arming a
    // cosmetically-folded "0.4.0" instead of "0.4.0rc14" would make apply
    // fail on the stable channel every time.
    const spy = stubFetch()
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      update_latest_version: '0.4.0rc14',
      update_channel: 'stable',
    } as never))
    mountWeb()

    const btn = await screen.findByRole('button', { name: /update to v0\.4\.0rc14/i })
    fireEvent.click(btn)

    await screen.findByTestId('in-app-update-armed')
    const armPost = spy.mock.calls.find(
      c => String(c[0]).includes('/api/update/arm') && (c[1] as { method?: string })?.method === 'POST',
    )
    expect(armPost).toBeTruthy()
  })

  it('does not render the flow when the backend did not probe the shape', async () => {
    stubFetch()
    store.dispatch(sseStatus({
      ...ARMABLE_STATUS,
      update_can_arm: false,
    } as never))
    mountWeb()

    // The manual instructions render instead.
    const manual = await screen.findByTestId('manual-update-instructions')
    expect(manual).toBeTruthy()
    expect(screen.queryByTestId('in-app-update')).toBeNull()
  })

  it('surfaces an arm refusal inline', async () => {
    stubFetch({ armError: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).toMatch(/verdict|refused|failed|409/i)
    // Still un-armed: the button stays available for a retry.
    expect(screen.queryByTestId('in-app-update-armed')).toBeNull()
  })

  it('narrates applying after the approval consumes the request', async () => {
    // shouldAdvanceTime keeps RTL waitFor/findBy live under fake timers;
    // without it every await inside testing-library stalls to its timeout.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      // armStatus answers armed:false — the approval landed in a terminal.
      const spy = stubFetch()
      spy.mockImplementation(async (input: unknown, init?: { method?: string }) => {
        const url = String(input)
        if (url.includes('/api/update/arm') && init?.method === 'POST') {
          return {
            ok: true, status: 200,
            json: async () => ({ ok: true, armed: true, request_id: 'r1', version: '9.9.9', expires_in: 600, approve_command: 'kirocrew update approve' }),
            text: async () => '', headers: new Headers({ 'content-type': 'application/json' }),
          }
        }
        if (url.includes('/api/update/arm')) {
          return {
            ok: true, status: 200,
            json: async () => ({ armed: false }),
            text: async () => '', headers: new Headers({ 'content-type': 'application/json' }),
          }
        }
        return {
          ok: true, status: 200, json: async () => ({}), text: async () => '',
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      })
      store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
      mountWeb()
      fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
      await screen.findByTestId('in-app-update-armed')
      // Let the 5s liveness poll fire; the state flip lands on the poll's
      // own microtask, so wait for the rerender rather than asserting inline.
      await vi.advanceTimersByTimeAsync(5100)
      await waitFor(() => expect(screen.getByTestId('in-app-update-applying')).toBeTruthy())
      // A progress push renders inline.
      store.dispatch(setUpdateProgress({ step: 'building', detail: 'Building the new environment…' } as never))
      await waitFor(() => expect(screen.getByTestId('apply-progress').textContent).toContain('Building'))
      // A failed push pins the failure with a retry — never a silent reset.
      store.dispatch(setUpdateProgress({ step: 'failed', detail: 'wheel SHA-256 mismatch' } as never))
      const failed = await screen.findByTestId('in-app-update-failed')
      expect(failed.textContent).toContain('wheel SHA-256 mismatch')
      expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('decides expiry vs approval by the absolute deadline, not the counter', async () => {
    // The wire cannot distinguish a consumed request from a TTL lapse: both
    // answer armed:false. A throttled background tab misses countdown ticks,
    // so only the absolute deadline separates "approval landed" (applying)
    // from "my own request expired" (expired).
    const { resolveUnarmedPhase } = await import('../pages/settings/AboutPanel')
    const deadline = 1_000_000
    expect(resolveUnarmedPhase(deadline, deadline - 1)).toBe('applying')
    expect(resolveUnarmedPhase(deadline, deadline)).toBe('expired')
    expect(resolveUnarmedPhase(deadline, deadline + 700_000)).toBe('expired')
  })

  it('clears a stale failed push when a fresh arm starts', async () => {
    // A prior attempt's `failed` progress push survives in the store. Without
    // clearing it on arm, the new armed panel is instantly bounced back to
    // the failure screen, making "Try again" a dead loop.
    stubFetch()
    store.dispatch(setUpdateProgress({ step: 'failed', detail: 'old failure' } as never))
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()
    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    await screen.findByTestId('in-app-update-armed')
    expect(screen.queryByTestId('in-app-update-failed')).toBeNull()
    expect(store.getState().dashboard.updateProgress).toBeNull()
  })

  it('keeps the manual installer command reachable behind the fold', async () => {
    stubFetch()
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    const flow = await screen.findByTestId('in-app-update')
    expect(flow.querySelector('details')).toBeTruthy()
    expect(flow.textContent).toContain('curl -fsSL')
  })
})
