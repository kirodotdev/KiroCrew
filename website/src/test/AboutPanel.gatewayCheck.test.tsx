//
// Contract under test — the gateway (non-Electron) "Check for updates" flow.
//
// The bug: the success line was rendered on `gwCheck.isSuccess && !showUpdate`,
// i.e. on any HTTP 200. For a wheel install the backend check never actually ran,
// so a check that did nothing told the user they were up to date while two
// releases behind. `checked` is now the verdict and a 200 is only transport.
//
// - check_status:'failed' + an error code -> failure line, NEVER the success line
// - an UNRECOGNISED error code     -> generic reason, still not the success line
// - check_status:'succeeded' + update_available:false -> the success line (the only
//   case that earns it)
// - succeeded + no update + commits_ahead>0 AND commits_behind>0 -> the DIVERGED
//   line (counts + rebase/merge instruction), never the success line and never
//   an Update button: `update_available:false` there is the no-auto-apply
//   safety property, not currency
// - available + !self_updatable    -> the installer command, and NO Update button
// - available + self_updatable     -> the Update button, unchanged
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

/** Route the component's three GETs; /api/update/check answers with `check`. */
function stubFetch(check: Record<string, unknown>) {
  const json = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/update/check')) return json(check)
    if (url.includes('/api/changelog')) return json({ content: '' })
    return json({})
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

function mountWeb() {
  // No window.updateAPI => isDesktop false => the gateway branch renders.
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

async function pressCheck() {
  const btn = await screen.findByRole('button', { name: /check for updates/i })
  fireEvent.click(btn)
}

/** A minimal-but-valid status payload; `sseStatus` dereferences it, so never null. */
const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

describe('AboutPanel gateway update check', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    // Reset the status the background-path tests push, so it cannot leak forward.
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('a check that could not run reports the failure, not "up to date"', async () => {
    stubFetch({ check_status: 'failed', update_available: null, error_code: 'feed_unreachable', managed_by: 'kirocrew' })
    mountWeb()
    await pressCheck()

    const failed = await screen.findByTestId('check-failed')
    expect(failed.textContent).toContain("Couldn't check for updates")
    expect(failed.textContent).toContain('release feed')
    expect(screen.queryByTestId('up-to-date')).toBeNull()
  })

  it('an unrecognised error code falls back to the generic reason', async () => {
    // A newer gateway paired with this bundle must still say the check failed.
    stubFetch({ check_status: 'failed', update_available: null, error_code: 'some_future_code' })
    mountWeb()
    await pressCheck()

    const failed = await screen.findByTestId('check-failed')
    expect(failed.textContent).toContain("The check didn't complete")
    expect(screen.queryByTestId('up-to-date')).toBeNull()
  })

  it('reports up to date only when a comparison actually completed', async () => {
    stubFetch({ check_status: 'succeeded', update_available: false, error_code: null, managed_by: 'kirocrew' })
    mountWeb()
    await pressCheck()

    const ok = await screen.findByTestId('up-to-date')
    expect(ok.textContent).toContain('latest version')
    expect(screen.queryByTestId('check-failed')).toBeNull()
  })

  it('a diverged checkout renders the counts and a rebase/merge instruction, not "up to date"', async () => {
    // update_available:false is the no-auto-apply property doing its job (the
    // apply path is a hard reset), so this state must read as diverged, never
    // as current — and it must not grow an Update button either.
    stubFetch({
      check_status: 'succeeded',
      update_available: false,
      error_code: null,
      managed_by: 'git',
      can_apply: true,
      commits_ahead: 3,
      commits_behind: 219,
    })
    mountWeb()
    await pressCheck()

    const diverged = await screen.findByTestId('diverged')
    expect(diverged.textContent).toContain('diverged')
    expect(diverged.textContent).toContain('3')
    expect(diverged.textContent).toContain('219')
    expect(diverged.textContent?.toLowerCase()).toContain('rebase')
    expect(screen.queryByTestId('up-to-date')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
    // The hero badge must not contradict the warning on the same screen: the
    // green "Up to date" pill yields to a warn "Diverged" pill.
    expect(screen.getByTestId('hero-diverged')).toBeTruthy()
    expect(screen.queryByTestId('hero-up-to-date')).toBeNull()
  })

  it('a checkout merely ahead (or behind) is not diverged: the success line stays', async () => {
    // Only the BOTH-non-zero pair means diverged. Ahead-only is the user's own
    // unpushed work and must keep reading as up to date, exactly as before the
    // counts existed on the wire.
    stubFetch({
      check_status: 'succeeded',
      update_available: false,
      error_code: null,
      managed_by: 'git',
      commits_ahead: 2,
      commits_behind: 0,
    })
    mountWeb()
    await pressCheck()

    const ok = await screen.findByTestId('up-to-date')
    expect(ok.textContent).toContain('latest version')
    expect(screen.queryByTestId('diverged')).toBeNull()
    // Ahead-only must not flip the hero badge either.
    expect(screen.queryByTestId('hero-diverged')).toBeNull()
    expect(screen.getByTestId('hero-up-to-date')).toBeTruthy()
  })

  it('a fresh diverged verdict outranks a stale redux update_available flag', async () => {
    // The redux flag refreshes on the slower WS status push, so a push carrying
    // `true` from a background check that ran before the checkout gained local
    // commits can land around a fresh manual check that says diverged. Letting
    // the flag win would render an Update button whose backend path is a bare
    // `git pull` — a silent merge into the user's branch — for up to one push
    // interval. The fresh check's diverged verdict must win: warning line, no
    // Update button, no update card.
    stubFetch({
      check_status: 'succeeded',
      update_available: false,
      error_code: null,
      managed_by: 'git',
      can_apply: true,
      commits_ahead: 3,
      commits_behind: 219,
    })
    mountWeb()
    await pressCheck()
    await screen.findByTestId('diverged')

    // The stale status push lands AFTER the fresh check's verdict.
    act(() => {
      store.dispatch(sseStatus({ ...BLANK_STATUS, update_available: true, update_can_apply: true } as never))
    })

    expect(await screen.findByTestId('diverged')).toBeTruthy()
    expect(screen.queryByTestId('up-to-date')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
    // The hero badge must show diverged too, not the stale "Update available".
    expect(screen.getByTestId('hero-diverged')).toBeTruthy()
  })

  it('the status push alone flips the hero badge to diverged on first visit', async () => {
    // Before any manual check the local counts are 0, so the badge reads the
    // background check's counts from the status push. Without them a fresh
    // visit to a diverged install painted the green "Up to date" pill — the
    // exact symptom the fix exists to kill, surviving one element over.
    stubFetch({})
    mountWeb()
    act(() => {
      store.dispatch(sseStatus({
        ...BLANK_STATUS,
        update_available: false,
        update_check_status: 'succeeded',
        update_commits_ahead: 3,
        update_commits_behind: 219,
      } as never))
    })

    expect(await screen.findByTestId('hero-diverged')).toBeTruthy()
    expect(screen.queryByTestId('hero-up-to-date')).toBeNull()
  })

  it('the confirm modal never offers apply while its pre-apply check is pending', async () => {
    // The check's answer may be "diverged"; an enabled apply during the wait
    // is a race the user can win against their own safety check.
    const never = new Promise<never>(() => {})
    const json = (body: unknown) => ({
      ok: true, status: 200, json: async () => body,
      text: async () => JSON.stringify(body),
      headers: new Headers({ 'content-type': 'application/json' }),
    })
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      const url = String(input)
      if (url.includes('/api/update/check')) return never
      if (url.includes('/api/changelog')) return json({ content: '' })
      return json({})
    }))
    store.dispatch(sseStatus({ ...BLANK_STATUS, update_available: true, update_can_apply: true } as never))
    mountWeb()

    const trigger = await screen.findByRole('button', { name: /update/i })
    fireEvent.click(trigger)

    const dialog = await screen.findByRole('dialog')
    // Scoped to the dialog: the page's own trigger button behind the backdrop
    // legitimately still exists in the DOM.
    expect(within(dialog).queryByRole('button', { name: /^Update now$/i })).toBeNull()
  })

  it('the confirm modal opened from a stale flag disarms once the check says diverged', async () => {
    // The other half of the same race: the stale flag renders the "Update to
    // vX" trigger, the user clicks it, and the modal's own pre-apply check
    // comes back diverged. The modal must explain and offer only Close — its
    // apply button POSTs /api/update, whose git path is a bare `git pull`.
    store.dispatch(sseStatus({ ...BLANK_STATUS, update_available: true, update_can_apply: true } as never))
    stubFetch({
      check_status: 'succeeded',
      update_available: false,
      error_code: null,
      managed_by: 'git',
      can_apply: true,
      commits_ahead: 3,
      commits_behind: 219,
    })
    mountWeb()

    const trigger = await screen.findByRole('button', { name: /update/i })
    fireEvent.click(trigger)

    const note = await screen.findByTestId('diverged-modal')
    expect(note.textContent?.toLowerCase()).toContain('rebase')
    expect(screen.queryByRole('button', { name: /^Update now$/i })).toBeNull()
    const dialog = screen.getByRole('dialog')
    expect(dialog).toBeTruthy()
    fireEvent.click(screen.getByTestId('diverged-modal-close'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('offers the installer command instead of a broken Update button on a wheel install', async () => {
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider"
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'kirocrew',
      can_apply: false,
      channel: 'insider',
      latest_version: '0.1.3rc2',
      remediation: { kind: 'command', message: '', command },
    })
    mountWeb()
    await pressCheck()

    const block = await screen.findByTestId('manual-update-command')
    // Verbatim, including the --channel the installer would otherwise default away
    // from. Rendered as text: nothing here is a link or interpolated markup.
    expect(block.textContent).toBe(command)
    expect(screen.getByTestId('manual-update-instructions').textContent).toContain('insider')
    // The Update button would 409 on this layout, so it must not be offered.
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
    expect(screen.getByRole('button', { name: /copy command/i })).toBeTruthy()
  })

  it('a command-managed gateway shows the policy note, never installer copy', async () => {
    // A check-only policy pin: an update is available but there is no in-app
    // apply. The self-managed installer instructions would tell the user to
    // run the exact mechanism the policy excluded (UX review finding).
    store.dispatch(sseStatus({ ...BLANK_STATUS, update_managed_by: 'command' } as never))
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      managed_by: 'command',
      can_apply: false,
      channel: '',
      latest_version: '2.0.0',
    })
    mountWeb()
    await pressCheck()
    await waitFor(() => expect(screen.getByTestId('policy-managed-update-note')).toBeTruthy())
    expect(screen.queryByTestId('manual-update-instructions')).toBeNull()
    expect(screen.queryByText(/re-running the installer/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
  })

  it('copying the command flips the button label', async () => {
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel stable"
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      managed_by: 'kirocrew',
      can_apply: false,
      channel: 'stable',
      remediation: { kind: 'command', message: '', command },
    })
    mountWeb()
    await pressCheck()

    fireEvent.click(await screen.findByRole('button', { name: /copy command/i }))
    expect(writeText).toHaveBeenCalledWith(command)
    await waitFor(() => expect(screen.getByRole('button', { name: /copied/i })).toBeTruthy())
  })

  it.each([
    ['managed_by_app', 'through the app'],
    ['managed_by_image', 'newer image'],
  ])('%s renders a neutral note, not a failure and not "up to date"', async (code, phrase) => {
    // The desktop bundles embed this backend, so they reach the gateway check and
    // defer to the Electron updater. Nothing failed, so "Couldn't check for
    // updates" would be a lie — but "up to date" would be worse.
    stubFetch({
      check_status: 'deferred',
      update_available: null,
      error_code: null,
      unavailable_reason: code,
      managed_by: 'electron',
    })
    mountWeb()
    await pressCheck()

    const note = await screen.findByTestId('check-not-applicable')
    expect(note.textContent).toContain(phrase)
    expect(screen.queryByTestId('up-to-date')).toBeNull()
    expect(screen.queryByTestId('check-failed')).toBeNull()
  })

  it('a git checkout still gets the Update button', async () => {
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'git',
      can_apply: true,
      latest_version: '0.1.3',
      changes: '### 0.1.3\n- thing',
    })
    mountWeb()
    await pressCheck()

    await waitFor(() => expect(screen.getByRole('button', { name: /^Update/ })).toBeTruthy())
    expect(screen.queryByTestId('manual-update-instructions')).toBeNull()
  })

  it('names the target version from latest_version', async () => {
    // The panel used to read `d.version`, which the gateway never emits — so the
    // "(vX)" suffix silently never appeared for a gateway install.
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      managed_by: 'git',
      can_apply: true,
      latest_version: '0.1.3rc2',
    })
    mountWeb()
    await pressCheck()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Update to v0\.1\.3rc2/ })).toBeTruthy(),
    )
  })

  // ---- the BACKGROUND-check path (no manual check run) ----------------------
  //
  // The 12-hourly gateway check lights the Settings nav dot. Following that badge
  // used to land on the primary "Update to vX" button even on a wheel install,
  // because the command only arrived from a manual check — a confirm dialog
  // ending in a raw 409, on every visit until the user happened to press Check.

  const pushStatus = (extra: Record<string, unknown>) =>
    store.dispatch(sseStatus({ ...BLANK_STATUS, ...extra } as never))

  it('a background-discovered wheel update offers the command, never the Update button', async () => {
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider"
    stubFetch({})
    pushStatus({
      update_available: true,
      update_can_apply: false,
      update_check_status: 'succeeded',
      update_command: command,
    })
    mountWeb()

    const block = await screen.findByTestId('manual-update-command')
    expect(block.textContent).toBe(command)
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
  })

  it('suppresses the Update button even when no command is known', async () => {
    // Fail safe: a false `can_apply` alone must disarm the button, with or
    // without a command to offer in its place.
    stubFetch({})
    pushStatus({ update_available: true, update_can_apply: false, update_check_status: 'succeeded' })
    mountWeb()

    await screen.findByTestId('manual-update-instructions')
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
    expect(screen.queryByTestId('manual-update-command')).toBeNull()
  })

  it('the hero pill stays neutral until a check has a verdict', async () => {
    stubFetch({})
    mountWeb()
    // Nothing checked yet: a green "Up to date" here would sit beside the very
    // "Couldn't check for updates" line this PR adds.
    expect(await screen.findByTestId('hero-not-checked')).toBeTruthy()
    expect(screen.queryByTestId('hero-up-to-date')).toBeNull()
  })

  it('the hero pill goes green once a check reports current', async () => {
    stubFetch({ check_status: 'succeeded', update_available: false, error_code: null })
    mountWeb()
    await pressCheck()
    await waitFor(() => expect(screen.getByTestId('hero-up-to-date')).toBeTruthy())
    expect(screen.queryByTestId('hero-not-checked')).toBeNull()
  })

  it('a failed check does NOT turn the hero pill green', async () => {
    stubFetch({ check_status: 'failed', update_available: null, error_code: 'feed_unreachable' })
    mountWeb()
    await pressCheck()
    await screen.findByTestId('check-failed')
    expect(screen.queryByTestId('hero-up-to-date')).toBeNull()
    expect(screen.getByTestId('hero-not-checked')).toBeTruthy()
  })

  it('the auto-apply toggle is reworded where the gateway cannot self-apply', async () => {
    stubFetch({})
    pushStatus({ update_can_apply: false, update_check_status: 'succeeded' })
    mountWeb()

    await waitFor(() =>
      expect(screen.getByText(/Notify when an update is available/)).toBeTruthy(),
    )
    // The auto-apply promise must not be shown where the backend downgrades it.
    expect(screen.queryByText(/Auto-update on restart/)).toBeNull()
  })

  it('the auto-apply toggle keeps its promise on a git checkout', async () => {
    stubFetch({})
    pushStatus({ update_can_apply: true, update_check_status: 'succeeded' })
    mountWeb()

    await waitFor(() => expect(screen.getByText(/Auto-update on restart/)).toBeTruthy())
  })

  it('copying awaits the clipboard helper before confirming', async () => {
    // navigator.clipboard is absent on a plain-HTTP remote gateway — exactly the
    // deployment this command targets — so the label must follow the helper's
    // fallback, not fire regardless.
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel stable"
    stubFetch({})
    pushStatus({
      update_available: true,
      update_can_apply: false,
      update_check_status: 'succeeded',
      update_command: command,
    })
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /copy command/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /copied/i })).toBeTruthy())
  })
})
