// In-app wheel update (arm + host-local approve) in Settings > About.
//
// Contract under test:
// - the Update button renders ONLY when the backend probed the managed-venv
//   shape (`update_can_arm`); managed_by alone must not summon it
// - clicking it POSTs /api/update/arm and swaps to the armed state showing
//   the approve command and countdown — and NEVER any nonce
// - an arm refusal surfaces as an inline error, not a dead button
// - the manual installer command stays reachable behind the details fold
import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus, setUpdateProgress } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel, InAppUpdateFlow } from '../pages/settings/AboutPanel'
import { __resetErrorJournalForTests, consumeChatHandoff } from '../utils/errorReport'
import { newQueryClient } from '../api/queryClient'

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

/** The gateway's `GET /api/update/arm` answer while an in-app (managed-venv) arm is live. */
const LIVE_ARM = {
  armed: true, managed_by: 'kirocrew', request_id: 'r1', version: '9.9.9',
  expires_in: 590, approve_command: 'kirocrew update approve',
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
      if (overrides.armStaleRefusal) {
        // The refusal the pre-arm re-check produces when the offer is gone. `text`
        // must carry the JSON: the panel reads the structured `code` off the raw
        // body, so a stubbed empty body would exercise a path the gateway never
        // sends and quietly prove nothing.
        const body = JSON.stringify(overrides.armStaleRefusal === 'withdrawn'
          ? { error: 'This update is no longer offered', code: 'arm_offer_withdrawn' }
          : {
            error: "This update is no longer offered — you're already up to date",
            code: 'arm_no_longer_offered',
          })
        return {
          ok: false, status: 409,
          json: async () => JSON.parse(body),
          text: async () => body,
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      }
      if (overrides.armFailed) {
        // A failure that is not about the offer, after the pre-arm re-check has
        // already refreshed the cached verdict to a newer version.
        const body = JSON.stringify({ error: 'could not record the pending update request: disk full', code: 'arm_failed' })
        return {
          ok: false, status: 500,
          json: async () => JSON.parse(body),
          text: async () => body,
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      }
      if (overrides.armError) {
        return {
          ok: false, status: 409,
          json: async () => ({ error: 'no update-available verdict', code: 'arm_no_verdict' }),
          text: async () => '',
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      }
      if (overrides.armRefusal) {
        // Any coded refusal, with the gateway's English `error` beside the code.
        const { status = 409, ...refusal } = overrides.armRefusal as { status?: number; code: string; error: string }
        const body = JSON.stringify(refusal)
        return {
          ok: false, status,
          json: async () => JSON.parse(body),
          text: async () => body,
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      }
      return json({
        ok: true, armed: true, request_id: 'r1',
        // The gateway arms whatever its own re-check found, which is not
        // necessarily the version the panel offered — see the stale-verdict
        // test below.
        version: (overrides.armVersion as string) ?? '9.9.9',
        version_display: (overrides.armVersionDisplay as string) ?? '9.9.9',
        expires_in: 600, approve_command: 'kirocrew update approve',
      })
    }
    if (url.includes('/api/update/arm')) {
      if (overrides.armStatus) return json((overrides.armStatus as () => unknown)())
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
    // A faked test that times out never reaches its own `finally`; restore
    // here so its fake clock cannot leak into the next test in the fork.
    vi.useRealTimers()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('renders the Update button for an armable install and arms on click', async () => {
    const spy = stubFetch()
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    const flow = await screen.findByTestId('in-app-update')
    expect(flow).toBeTruthy()
    const btn = screen.getByRole('button', { name: /update to v9\.9\.9/i })
    expect(btn).toBe(screen.getByTestId('in-app-update-action'))
    fireEvent.click(btn)

    const armed = await screen.findByTestId('in-app-update-armed')
    expect(armed).toBeTruthy()
    const armedAction = screen.getByTestId('in-app-update-action')
    expect(armedAction).toBe(btn)
    expect(armedAction).toHaveTextContent(/copy command/i)
    expect(armed).toHaveTextContent(/gateway host/i)
    expect(screen.getByTestId('approve-command').textContent).toContain('kirocrew update approve')
    // The countdown renders from expires_in.
    expect(screen.getByTestId('arm-countdown').textContent).toMatch(/10:00|9:5\d/)
    // The arm POST went out; nothing in the DOM carries a nonce-shaped secret.
    const armPost = spy.mock.calls.find(
      c => String(c[0]).includes('/api/update/arm') && (c[1] as { method?: string })?.method === 'POST',
    )
    expect(armPost).toBeTruthy()
    // The click names the version its button offered: the gateway reads it to
    // tell an upgrade click from a channel-move click.
    expect(JSON.parse(String((armPost![1] as { body?: unknown }).body))).toEqual({ offered: '9.9.9' })
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

  it('names the version the gateway ACTUALLY armed, not the stale one the button offered', async () => {
    // The panel's offer comes from a verdict refreshed every 12 hours, while the
    // arm re-checks the feed so the approval installs the NEWEST build. When a
    // newer release published in between, the two disagree — and the one that
    // installs is the armed one, so that is the one the armed copy must name.
    stubFetch({ armVersion: '9.9.10', armVersionDisplay: '9.9.10' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    await screen.findByTestId('in-app-update-armed')
    // When the two disagree the explanatory note carries the armed version — its
    // copy opens by naming it — and the plain "Update to v…" line stands down, so
    // the panel states that number once instead of twice.
    expect(screen.getByTestId('armed-version-changed').textContent).toMatch(/9\.9\.10/)
    expect(screen.queryByTestId('armed-version')).toBeNull()
  })

  it('explains the version change instead of leaving two different numbers on screen', async () => {
    // Naming the armed version is only half the job: the user clicked a button
    // reading v9.9.9 and is now looking at v9.9.10, seconds before running an
    // approval command on another machine. Unexplained, that reads as two
    // different updates or as one of the numbers being wrong, and the safe
    // response to either is to abandon a correct arm.
    stubFetch({ armVersion: '9.9.10', armVersionDisplay: '9.9.10' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    await screen.findByTestId('in-app-update-armed')
    const note = screen.getByTestId('armed-version-changed').textContent || ''
    expect(note).toMatch(/9\.9\.10/)
    expect(note).toMatch(/9\.9\.9/)
  })

  it('stays quiet when the armed version is the one the button offered', async () => {
    // Nothing to explain on the ordinary path, and a note that fires anyway would
    // teach the user to ignore it on the one path that needs it.
    stubFetch({ armVersion: '9.9.9', armVersionDisplay: '9.9.9' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    await screen.findByTestId('in-app-update-armed')
    expect(screen.getByTestId('armed-version').textContent).toMatch(/9\.9\.9/)
    expect(screen.queryByTestId('armed-version-changed')).toBeNull()
  })

  it('keeps the explanation when the status frame catches up to the armed version', async () => {
    // The arm's own re-check refreshes the cached verdict, so the next status frame
    // moves the offered version to the armed one. The note compares against what
    // the button said WHEN CLICKED, or it would vanish while being read.
    stubFetch({ armVersion: '9.9.10', armVersionDisplay: '9.9.10' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    await screen.findByTestId('armed-version-changed')

    await act(async () => {
      store.dispatch(sseStatus({ ...ARMABLE_STATUS, update_latest_version: '9.9.10' } as never))
    })

    const note = screen.getByTestId('armed-version-changed').textContent || ''
    expect(note).toMatch(/9\.9\.10/)
    expect(note).toMatch(/9\.9\.9/)
  })

  it('does not call an OLDER armed build "a newer version"', async () => {
    // The re-check can arm a lower build than the button named (the offered
    // release was pulled; an older one is still ahead of the running build). The
    // "a newer version was found" note would then be false, so the plain line,
    // which names what installs and claims nothing about why, renders instead.
    stubFetch({ armVersion: '9.9.8', armVersionDisplay: '9.9.8' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    await screen.findByTestId('in-app-update-armed')
    expect(screen.queryByTestId('armed-version-changed')).toBeNull()
    expect(screen.getByTestId('armed-version').textContent).toMatch(/9\.9\.8/)
  })

  it('omits the armed-version line for a gateway that predates the field', async () => {
    stubFetch({ armVersionDisplay: '' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    await screen.findByTestId('in-app-update-armed')
    expect(screen.queryByTestId('armed-version')).toBeNull()
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

  it('stops offering an update the gateway just refused as gone', async () => {
    // The pre-arm re-check is what discovers the offer is stale, so the refusal is
    // a FRESHER fact than the status frame that produced the button. Leaving the
    // button up means the panel says "you're already up to date" directly beneath
    // an action to install an update — a blind reader shown that state could not
    // tell which statement to believe, and the button is the dangerous half,
    // because clicking it can only fail the same way again.
    stubFetch({ armStaleRefusal: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).toMatch(/no longer offered/i)
    // The contradiction is gone: no action to install what does not exist.
    expect(screen.queryByRole('button', { name: /update to v9\.9\.9/i })).toBeNull()
    // And the way out is still on screen — this state is not a dead end.
    expect(screen.getByText(/or re-run the installer manually/i)).toBeTruthy()
  })

  it('does not claim "already up to date" when the clicked upgrade became a downgrade', async () => {
    // The lane was rolled back below the running build: the clicked offer is gone,
    // but the next status frame offers a channel move, so the refusal names only
    // the withdrawal.
    stubFetch({ armStaleRefusal: 'withdrawn' })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))

    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).toMatch(/no longer offered/i)
    expect(err.textContent).not.toMatch(/up to date/i)
    expect(screen.queryByRole('button', { name: /update to v9\.9\.9/i })).toBeNull()

    // The next status frame carries the channel move the notice announces: the
    // notice stays to explain why the offer changed, and the new offer appears.
    await act(async () => {
      store.dispatch(sseStatus({
        ...ARMABLE_STATUS,
        update_available: false,
        update_channel_move_pending: true,
        update_latest_version: '9.9.6',
      } as never))
    })
    expect(screen.getByTestId('arm-error').textContent).toMatch(/no longer offered/i)
    expect(screen.getByRole('button', { name: /switch to v9\.9\.6/i })).toBeTruthy()
  })

  it('a new offer retires the "no longer offered" notice', async () => {
    stubFetch({ armStaleRefusal: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    expect((await screen.findByTestId('arm-error')).textContent).toMatch(/no longer offered/i)

    // A newer release ships: the notice would contradict the button now offering it.
    await act(async () => {
      store.dispatch(sseStatus({ ...ARMABLE_STATUS, update_latest_version: '9.9.10' } as never))
    })
    expect(screen.queryByTestId('arm-error')).toBeNull()
    expect(screen.getByRole('button', { name: /update to v9\.9\.10/i })).toBeTruthy()
  })

  it('keeps a failed arm\'s reason when its own re-check moves the offered version', async () => {
    // The arm re-checks before it fails, so the next status frame offers the
    // version that re-check found. The failure is not about the offer, and
    // erasing it would leave a new button with no word on why the click failed.
    stubFetch({ armFailed: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    expect((await screen.findByTestId('arm-error')).textContent).toMatch(/couldn't prepare the update/i)

    await act(async () => {
      store.dispatch(sseStatus({ ...ARMABLE_STATUS, update_latest_version: '9.9.10' } as never))
    })
    expect(screen.getByTestId('arm-error').textContent).toMatch(/couldn't prepare the update/i)
    expect(screen.getByRole('button', { name: /update to v9\.9\.10/i })).toBeTruthy()
  })

  it('keeps the offer up when the arm\'s own re-check failed, so the click can be retried', async () => {
    // A failed re-check says nothing about the offer: it is not void, and trying
    // again is the remedy, so the button must stay.
    stubFetch({
      armRefusal: { code: 'arm_check_failed', error: 'the update check failed, so nothing was armed — try again' },
    })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).toMatch(/couldn't check for the latest version/i)
    // The remedy names the button by its own label, so the notice above it does
    // not read as "this button fails".
    expect(err.textContent).toContain('“Update to v9.9.9”')
    expect(err.textContent).not.toMatch(/run a check first|nothing was armed/i)
    expect(screen.getByRole('button', { name: /update to v9\.9\.9/i })).not.toBeDisabled()
  })

  for (const [code, line] of [
    ['arm_no_verdict', /no update is ready to install/i],
    ['arm_below_min_version', /older than the minimum version/i],
    ['arm_policy_managed', /managed by an operator-defined update policy/i],
    ['arm_wrong_shape', /works only for the cli\.sh install/i],
  ] as const) {
    it(`words the ${code} refusal from the catalog, not the gateway's English`, async () => {
      stubFetch({ armRefusal: { code, error: 'GATEWAY ENGLISH DETAIL' } })
      store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
      mountWeb()

      fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
      const err = await screen.findByTestId('arm-error')
      expect(err.textContent).toMatch(line)
      expect(err.textContent).not.toMatch(/GATEWAY ENGLISH DETAIL/)
    })
  }

  it('hands the agent the gateway\'s report for a refusal worded from the catalog', async () => {
    // The transport files the report under the gateway's message, which the
    // notice no longer shows, so the notice must carry the report itself.
    __resetErrorJournalForTests()
    stubFetch({ armFailed: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).not.toMatch(/disk full/)
    fireEvent.click(within(err).getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('HTTP 500')
    expect(prompt).toContain('arm_failed')
    expect(prompt).toContain('disk full')
  })

  it('shows the gateway\'s own message for a refusal code this bundle does not know', async () => {
    stubFetch({ armRefusal: { code: 'arm_some_future_refusal', error: 'a newer gateway explains itself' } })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    expect((await screen.findByTestId('arm-error')).textContent).toMatch(/a newer gateway explains itself/)
  })

  it('surfaces an arm refusal inline', async () => {
    stubFetch({ armError: true })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
    const err = await screen.findByTestId('arm-error')
    expect(err.textContent).toMatch(/verdict|refused|failed|409/i)
    // Still un-armed: the button stays available for a retry. A refusal that is NOT
    // "the offer is gone" is worth retrying, so this is the opposite of the case
    // above and the two must not converge.
    expect(screen.queryByTestId('in-app-update-armed')).toBeNull()
    expect(screen.getByRole('button', { name: /update to v9\.9\.9/i })).toBeTruthy()
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

  it('shows a staged install as done, not as an update that ended without starting', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const spy = stubFetch()
      spy.mockImplementation(async (input: unknown, init?: { method?: string }) => {
        const url = String(input)
        const reply = (body: unknown) => ({
          ok: true, status: 200,
          json: async () => body, text: async () => '',
          headers: new Headers({ 'content-type': 'application/json' }),
        })
        if (url.includes('/api/update/arm') && init?.method === 'POST') {
          return reply({ ok: true, armed: true, request_id: 'r1', version: '9.9.9', expires_in: 600, approve_command: 'kirocrew update approve' })
        }
        if (url.includes('/api/update/arm')) return reply({ armed: false })
        return reply({})
      })
      store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
      mountWeb()
      fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
      await screen.findByTestId('in-app-update-armed')
      await vi.advanceTimersByTimeAsync(5100)
      await waitFor(() => expect(screen.getByTestId('in-app-update-applying')).toBeTruthy())
      store.dispatch(setUpdateProgress({ step: 'building', detail: 'Building the new environment…' } as never))
      // The gateway's staged success: the new version is installed beside the
      // running one and starts at the next restart.
      store.dispatch(setUpdateProgress({ step: 'done', detail: 'v9.9.9 installed. Restart to use it.' } as never))
      await waitFor(() => expect(screen.getByTestId('in-app-update-staged')).toBeTruthy())
      // Past the grace window that used to turn a silent apply into "ended".
      await vi.advanceTimersByTimeAsync(60_000)
      expect(screen.queryByTestId('arm-ended-note')).toBeNull()
      expect(screen.getByTestId('in-app-update-staged')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves applying when the arm went away early and no update started', async () => {
    // A dismissed request, or a gateway that restarted and forgot the arm, also
    // answers armed:false before the deadline. Only an approval starts an apply,
    // so with no progress push in the grace window the panel must not sit on a
    // disabled "Applying" button forever.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const spy = stubFetch()
      spy.mockImplementation(async (input: unknown, init?: { method?: string }) => {
        const url = String(input)
        const body = url.includes('/api/update/arm') && init?.method === 'POST'
          ? { ok: true, armed: true, request_id: 'r1', version: '9.9.9', expires_in: 600, approve_command: 'kirocrew update approve' }
          : url.includes('/api/update/arm') ? { armed: false } : {}
        return {
          ok: true, status: 200, json: async () => body, text: async () => '',
          headers: new Headers({ 'content-type': 'application/json' }),
        }
      })
      const { APPLY_START_GRACE_MS } = await import('../pages/settings/AboutPanelUpdateFlow')
      store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
      mountWeb()
      fireEvent.click(await screen.findByRole('button', { name: /update to v9\.9\.9/i }))
      await screen.findByTestId('in-app-update-armed')
      await vi.advanceTimersByTimeAsync(5100)
      await waitFor(() => expect(screen.getByTestId('in-app-update-applying')).toBeTruthy())
      expect(screen.getByTestId('in-app-update-action')).toBeDisabled()
      await vi.advanceTimersByTimeAsync(APPLY_START_GRACE_MS + 100)
      await waitFor(() => expect(screen.getByTestId('in-app-update')).toBeTruthy())
      expect(screen.getByTestId('arm-ended-note')).toBeTruthy()
      expect(screen.getByRole('button', { name: /update to v9\.9\.9/i })).not.toBeDisabled()
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

  // These run on the dashboard's own client: its cached answers never go stale,
  // which is what a copy mounted later has to read past.
  const withProviders = (qc: QueryClient, ui: ReactNode) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const isArmPost = (c: unknown[]) =>
    String(c[0]).includes('/api/update/arm') && (c[1] as { method?: string })?.method === 'POST'

  it('shows an arm another surface made instead of offering a second one', async () => {
    let serverArmed = false
    const spy = stubFetch({ armStatus: () => (serverArmed ? LIVE_ARM : { armed: false }) })
    store.dispatch(sseStatus({ ...ARMABLE_STATUS } as never))
    const qc = newQueryClient()
    // What's new arms the update, then closes.
    const whatsNew = render(withProviders(qc, <InAppUpdateFlow version="9.9.9" manualCommand="" />))
    await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))
    serverArmed = true
    fireEvent.click(screen.getByRole('button', { name: /update to v9\.9\.9/i }))
    await screen.findByTestId('in-app-update-armed')
    whatsNew.unmount()

    // Settings > About mounts its own copy afterwards, while the gateway holds that arm.
    render(withProviders(qc, <AboutPanel />))
    const armed = await screen.findByTestId('in-app-update-armed')
    expect(within(armed).getByTestId('approve-command').textContent).toContain('kirocrew update approve')
    expect(spy.mock.calls.filter(isArmPost)).toHaveLength(1)
  })

  it('shows one copy\'s arm on another copy that is on screen at the same time', async () => {
    let serverArmed = false
    stubFetch({ armStatus: () => (serverArmed ? LIVE_ARM : { armed: false }) })
    const qc = newQueryClient()
    render(withProviders(qc, (
      <>
        <div data-testid="copy-a"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>
        <div data-testid="copy-b"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>
      </>
    )))
    // Both idle copies have read "nothing armed" before the click.
    await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))
    serverArmed = true
    const copyA = screen.getByTestId('copy-a')
    const copyB = screen.getByTestId('copy-b')
    fireEvent.click(within(copyA).getByRole('button', { name: /update to v9\.9\.9/i }))

    await waitFor(() => expect(within(copyB).getByTestId('in-app-update-armed')).toBeTruthy())
    expect(within(copyB).getByTestId('approve-command').textContent).toContain('kirocrew update approve')
    // The copy that armed is waiting for the approval: the earlier "nothing armed"
    // read is not taken for that approval landing.
    expect(within(copyA).getByTestId('in-app-update-armed')).toBeTruthy()
  })

  it('shows an adopted arm instead of an earlier attempt\'s failure', async () => {
    // A failed apply's progress stays in the store (a new tab reads it off the
    // status), but a live arm postdates that attempt.
    stubFetch({ armStatus: () => LIVE_ARM })
    store.dispatch(setUpdateProgress({ step: 'failed', detail: 'old failure' } as never))
    try {
      render(withProviders(newQueryClient(), <InAppUpdateFlow version="9.9.9" manualCommand="" />))
      await waitFor(() => expect(store.getState().dashboard.updateProgress).toBeNull())
      const armed = await screen.findByTestId('in-app-update-armed')
      expect(within(armed).getByTestId('approve-command').textContent).toContain('kirocrew update approve')
      expect(screen.queryByTestId('in-app-update-failed')).toBeNull()
    } finally {
      store.dispatch(setUpdateProgress(null))
    }
  })

  it('does not take a read that began before the arm for its approval', async () => {
    // The mount-time read is still out when the click arms; its `armed: false`
    // predates the arm, so landing after it would read as the approval.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let releaseMountRead!: (body: unknown) => void
      const mountRead = new Promise(resolve => { releaseMountRead = resolve })
      let reads = 0
      stubFetch({ armStatus: () => (reads++ === 0 ? mountRead : LIVE_ARM) })
      const qc = newQueryClient()
      render(withProviders(qc, <InAppUpdateFlow version="9.9.9" manualCommand="" />))
      fireEvent.click(screen.getByRole('button', { name: /update to v9\.9\.9/i }))
      await screen.findByTestId('in-app-update-armed')
      releaseMountRead({ armed: false })

      // The next liveness poll still finds the arm, and the panel is still waiting.
      await vi.advanceTimersByTimeAsync(5100)
      await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual(LIVE_ARM))
      expect(screen.getByTestId('in-app-update-armed')).toBeTruthy()
      expect(screen.getByTestId('approve-command').textContent).toContain('kirocrew update approve')
    } finally {
      vi.useRealTimers()
    }
  })

  it('reads the arm again on Try again, so an arm made by another copy\'s retry shows', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let server: unknown = { armed: false }
      const spy = stubFetch({ armStatus: () => server })
      const qc = newQueryClient()
      render(withProviders(qc, <div data-testid="about"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>))
      const overlayRoot = render(withProviders(qc, <div data-testid="overlay"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>))
      const about = screen.getByTestId('about')
      const overlay = screen.getByTestId('overlay')
      await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))

      // About arms, the overlay shows that arm, the approval lands, and the apply fails.
      server = LIVE_ARM
      fireEvent.click(within(about).getByRole('button', { name: /update to v9\.9\.9/i }))
      await waitFor(() => expect(within(overlay).getByTestId('in-app-update-armed')).toBeTruthy())
      server = { armed: false }
      await vi.advanceTimersByTimeAsync(5100)
      await waitFor(() => expect(within(about).getByTestId('in-app-update-applying')).toBeTruthy())
      await waitFor(() => expect(within(overlay).getByTestId('in-app-update-applying')).toBeTruthy())
      store.dispatch(setUpdateProgress({ step: 'failed', detail: 'wheel SHA-256 mismatch' } as never))
      await waitFor(() => expect(within(about).getByTestId('in-app-update-failed')).toBeTruthy())
      await waitFor(() => expect(within(overlay).getByTestId('in-app-update-failed')).toBeTruthy())

      // The overlay retries and arms again, then closes.
      fireEvent.click(within(overlay).getByRole('button', { name: /try again/i }))
      server = LIVE_ARM
      fireEvent.click(within(overlay).getByRole('button', { name: /update to v9\.9\.9/i }))
      await waitFor(() => expect(within(overlay).getByTestId('in-app-update-armed')).toBeTruthy())
      overlayRoot.unmount()

      // Try again in About, later than every answer already cached.
      vi.setSystemTime(Date.now() + 1000)
      fireEvent.click(within(about).getByRole('button', { name: /try again/i }))
      await waitFor(() => expect(within(about).getByTestId('in-app-update-armed')).toBeTruthy())
      expect(within(about).getByTestId('approve-command').textContent).toContain('kirocrew update approve')
      expect(spy.mock.calls.filter(isArmPost)).toHaveLength(2)
    } finally {
      vi.useRealTimers()
      store.dispatch(setUpdateProgress(null))
    }
  })

  it('shows an arm made elsewhere after this copy\'s own arm expired', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let server: unknown = { armed: false }
      const spy = stubFetch({ armStatus: () => server })
      const qc = newQueryClient()
      render(withProviders(qc, <div data-testid="about"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>))
      const about = screen.getByTestId('about')
      await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))
      fireEvent.click(within(about).getByRole('button', { name: /update to v9\.9\.9/i }))
      await within(about).findByTestId('in-app-update-armed')

      // The arm lapses unapproved. The cache still holds this copy's own live
      // answer from before the lapse, which must not bring it back.
      vi.setSystemTime(Date.now() + 601_000)
      await vi.advanceTimersByTimeAsync(1000)
      await within(about).findByTestId('arm-expired-note')

      // The update-found dialog opens, reads "nothing armed", arms, and closes.
      const dialogRoot = render(withProviders(qc, <div data-testid="dialog"><InAppUpdateFlow version="9.9.9" manualCommand="" /></div>))
      await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))
      expect(within(about).getByTestId('arm-expired-note')).toBeTruthy()
      expect(within(about).queryByTestId('in-app-update-armed')).toBeNull()
      server = LIVE_ARM
      fireEvent.click(within(screen.getByTestId('dialog')).getByRole('button', { name: /update to v9\.9\.9/i }))
      await waitFor(() => expect(within(about).getByTestId('in-app-update-armed')).toBeTruthy())
      dialogRoot.unmount()

      expect(within(about).getByTestId('approve-command').textContent).toContain('kirocrew update approve')
      expect(spy.mock.calls.filter(isArmPost)).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reads the arm again when its own arm ends without an update', async () => {
    // An arm made outside this client (another tab) writes no seed here, so the
    // copy only learns of it by reading again as the offer comes back.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let server: unknown = { armed: false }
      const spy = stubFetch({ armStatus: () => server })
      const { APPLY_START_GRACE_MS } = await import('../pages/settings/AboutPanelUpdateFlow')
      const qc = newQueryClient()
      render(withProviders(qc, <InAppUpdateFlow version="9.9.9" manualCommand="" />))
      await waitFor(() => expect(qc.getQueryData(['update-arm-status'])).toEqual({ armed: false }))
      fireEvent.click(screen.getByRole('button', { name: /update to v9\.9\.9/i }))
      await screen.findByTestId('in-app-update-armed')

      // The arm goes away before its deadline with no apply, and another tab arms.
      await vi.advanceTimersByTimeAsync(5100)
      await screen.findByTestId('in-app-update-applying')
      server = LIVE_ARM
      await vi.advanceTimersByTimeAsync(APPLY_START_GRACE_MS + 100)

      await screen.findByTestId('in-app-update-armed')
      expect(screen.getByTestId('approve-command').textContent).toContain('kirocrew update approve')
      expect(spy.mock.calls.filter(isArmPost)).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
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
