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
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18nT } from '../i18n/t'

type Answer = { status: number; body: unknown }

/** Route the component's requests; /api/update/check answers with `check`. */
function stubFetch(
  check: Record<string, unknown>,
  { config = () => Promise.resolve({ status: 200, body: { auto_update: true } }), autoUpdateSave }: {
    config?: () => Promise<Answer>
    autoUpdateSave?: (body: Record<string, unknown>) => Answer
  } = {},
) {
  const json = ({ status, body }: Answer) => ({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/update/check')) return json({ status: 200, body: check })
    if (url.includes('/api/config/kirocrew')) return json(await config())
    if (url.includes('/api/update/auto')) {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
      return json(autoUpdateSave ? autoUpdateSave(body) : { status: 200, body: { ok: true, auto_update: body.enabled } })
    }
    if (url.includes('/api/changelog')) return json({ status: 200, body: { content: '' } })
    return json({ status: 200, body: {} })
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

function mountWeb() {
  // No window.updateAPI => isDesktop false => the gateway branch renders.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { qc, ...render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  ) }
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

  it('the version chip shows the folded running version, raw fallback for an older gateway', async () => {
    // A promoted stable build's bytes keep the RC stamp; the gateway folds it
    // into `version_display` and the chip must render THAT, never the raw
    // `version` (which stays raw for the SPA's reload-on-upgrade comparison).
    stubFetch({ check_status: 'succeeded', update_available: false, error_code: null })
    store.dispatch(sseStatus({
      ...BLANK_STATUS, version: '0.4.0rc14', version_display: '0.4.0',
    } as never))
    mountWeb()
    expect(await screen.findByText('v0.4.0')).toBeTruthy()
    expect(screen.queryByText('v0.4.0rc14')).toBeNull()
    cleanup()

    // An older gateway sends no `version_display`: the chip falls back to the
    // raw version rather than rendering an empty chip.
    store.dispatch(sseStatus({ ...BLANK_STATUS, version: '0.4.0rc14' } as never))
    mountWeb()
    expect(await screen.findByText('v0.4.0rc14')).toBeTruthy()
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
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
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
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
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

    const trigger = await screen.findByRole('button', { name: /^Update(?! the gateway)/ })
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

    const trigger = await screen.findByRole('button', { name: /^Update(?! the gateway)/ })
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
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
    expect(screen.getByRole('button', { name: /copy command/i })).toBeTruthy()
  })

  it('uses fresh check arm capability before the status frame catches up', async () => {
    const command = "curl -fsSL https://download.crew.kiro.dev/cli.sh | sh"
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      update_can_arm: false,
      update_managed_by: 'kirocrew',
    } as never))
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'kirocrew',
      can_apply: false,
      can_arm: true,
      channel: 'stable',
      latest_version: '0.7.0',
      remediation: { kind: 'command', message: '', command },
    })
    mountWeb()
    await pressCheck()

    expect(await screen.findByTestId('in-app-update')).toBeTruthy()
    expect(screen.getByRole('button', { name: /update to v0\.7\.0/i })).toBeTruthy()
    expect(screen.queryByTestId('manual-update-instructions')).toBeNull()
  })

  it('uses fresh ineligible check capability over stale armable status', async () => {
    const command = "curl -fsSL https://download.crew.kiro.dev/cli.sh | sh"
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      update_can_arm: true,
      update_managed_by: 'kirocrew',
    } as never))
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'kirocrew',
      can_apply: false,
      can_arm: false,
      channel: 'stable',
      latest_version: '0.7.0',
      remediation: { kind: 'command', message: '', command },
    })
    mountWeb()
    await pressCheck()

    expect(await screen.findByTestId('manual-update-instructions')).toBeTruthy()
    expect(screen.queryByTestId('in-app-update')).toBeNull()
  })

  it('the available-version line shows the folded display value, keeping the raw stamp off screen', async () => {
    // A promoted stable candidate keeps its rc stamp in latest_version (that is
    // what arm/apply key on); the check response carries the folded sibling
    // latest_version_display for the human-facing line. The manual-check
    // handler must adopt it -- this is the path the About panel's "a new
    // version (vX) is available" text renders from.
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'kirocrew',
      can_apply: false,
      channel: 'stable',
      latest_version: '0.4.0rc14',
      latest_version_display: '0.4.0',
      remediation: { kind: 'command', message: '', command: 'kirocrew update' },
    })
    mountWeb()
    await pressCheck()

    await waitFor(() => {
      const body = document.body.textContent || ''
      expect(body).toContain('(v0.4.0)')
      expect(body).not.toContain('0.4.0rc14')
    })
  })

  it('falls back to the raw version when an older gateway omits the display sibling', async () => {
    stubFetch({
      check_status: 'succeeded',
      update_available: true,
      error_code: null,
      managed_by: 'kirocrew',
      can_apply: false,
      channel: 'stable',
      latest_version: '0.4.0rc14',
      remediation: { kind: 'command', message: '', command: 'kirocrew update' },
    })
    mountWeb()
    await pressCheck()

    await waitFor(() => {
      expect(document.body.textContent || '').toContain('(v0.4.0rc14)')
    })
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
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
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

    await waitFor(() => expect(screen.getByRole('button', { name: /^Update(?! the gateway)/ })).toBeTruthy())
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
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
  })

  it('suppresses the Update button even when no command is known', async () => {
    // Fail safe: a false `can_apply` alone must disarm the button, with or
    // without a command to offer in its place.
    stubFetch({})
    pushStatus({ update_available: true, update_can_apply: false, update_check_status: 'succeeded' })
    mountWeb()

    await screen.findByTestId('manual-update-instructions')
    expect(screen.queryByRole('button', { name: /^Update(?! the gateway)/ })).toBeNull()
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

  // The gateway's `auto_update` row is one SettingsToggle in every state. The
  // gateway's `update_auto_effect` decides whether it can be flipped and the
  // note under it (`gatewayAutoUpdateCopy`, unit-tested on its own).
  const LABEL = i18nT('pages.settings.aboutPanel.automatic_updates')
  const NOTIFY_ONLY = i18nT('pages.settings.aboutPanel.auto_update_notify_only_on_this_install')
  const autoUpdateRow = () => document.querySelector<HTMLElement>('[data-setting-key="auto_update"]')
  const note = () => screen.queryByTestId('auto-update-note')
  // The Toggle is a role="switch" div, so its held state is `aria-disabled`.
  const live = () => expect(screen.getByRole('switch', { name: LABEL })).not.toHaveAttribute('aria-disabled')
  const held = () => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-disabled', 'true')

  it('offers a live switch where the gateway installs, with no note', async () => {
    stubFetch({})
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()

    await waitFor(live)
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true')
    expect(note()).toBeNull()
  })

  it('keeps the same row, held off with its reason, when the effect turns to notify', async () => {
    // A verdict can land while the row is on screen; the row changes state in
    // place rather than being swapped for another element.
    stubFetch({})
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)
    const row = autoUpdateRow()

    act(() => { pushStatus({ update_auto_effect: 'notify' }) })
    await waitFor(held)
    expect(note()).toHaveTextContent(NOTIFY_ONLY)
    expect(autoUpdateRow()).toBe(row)
  })

  it.each([
    ['mandatory', 'pages.settings.aboutPanel.gateway_auto_update_mandatory_note'],
    // A gateway without the field has not said what the switch does.
    [undefined, 'pages.settings.aboutPanel.gateway_auto_update_unknown_hint'],
  ])('notes the %s effect beside a live switch', async (effect, key) => {
    stubFetch({})
    pushStatus({ update_auto_effect: effect })
    mountWeb()

    await waitFor(live)
    expect(note()).toHaveTextContent(i18nT(key))
  })

  it('carries both deep-link anchors from the first paint, before the saved value is read', async () => {
    // `key:auto_update` (the agent's route) resolves on the key, the palette's id
    // form on the registry label; a search that probes once must find the row.
    stubFetch({}, { config: () => new Promise(() => {}) })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()

    await waitFor(() => expect(autoUpdateRow()).not.toBeNull())
    held()
    const entry = SETTINGS_REGISTRY.find(e => e.configKey === 'auto_update')
    expect(autoUpdateRow()?.dataset.settingLabel).toBe(i18nT(entry!.labelKey!))
  })

  it("takes the saved value from a check's answer, so a change made elsewhere appears", async () => {
    stubFetch({ check_status: 'succeeded', update_available: false, auto_update: false })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true'))

    await pressCheck()
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
  })

  it("does not take a check's value when the switch was saved while it ran", async () => {
    // The check read the config before the save landed, so its copy is older.
    let answerCheck: () => void = () => {}
    let saved = true
    const spy = stubFetch({}, {
      config: () => Promise.resolve({ status: 200, body: { auto_update: saved } }),
      autoUpdateSave: body => { saved = body.enabled as boolean; return { status: 200, body: { ok: true, auto_update: saved } } },
    })
    const route = spy.getMockImplementation()!
    spy.mockImplementation(async (input: unknown, init?: RequestInit) => {
      if (!String(input).includes('/api/update/check')) return route(input, init)
      await new Promise<void>(resolve => { answerCheck = resolve })
      return route(input, init).then(r => ({ ...r, json: async () => ({ check_status: 'succeeded', auto_update: true }) }))
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)

    await pressCheck()
    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
    await act(async () => { answerCheck() })
    await waitFor(() => expect(screen.queryByRole('button', { name: /check for updates/i })).not.toBeDisabled())
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false')
  })

  it('keeps the switch usable at its default after a failed read', async () => {
    stubFetch({}, { config: () => Promise.resolve({ status: 500, body: { error: 'zzq' } }) })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()

    expect(await screen.findByTestId('auto-update-read-error')).toBeTruthy()
    live()
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true')
  })

  it('moves on a click after a failed read, and keeps what the save answered', async () => {
    // Nothing was read, so there is no cached config to write into; the
    // switch shows the click while it saves and the answer after.
    stubFetch({}, { config: () => Promise.resolve({ status: 500, body: { error: 'zzq' } }) })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    expect(await screen.findByTestId('auto-update-read-error')).toBeTruthy()

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
    await waitFor(() => expect(screen.queryByTestId('auto-update-save-error')).toBeNull())
    // Settled, and still where the gateway said it is.
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false')
  })

  it('after a failed read, a failed save falls back to the last value a save confirmed', async () => {
    // The gateway is off after the first save; the second never landed, so
    // the switch must not drop to the default, on, beside its save error.
    let saves = 0
    stubFetch({}, {
      config: () => Promise.resolve({ status: 500, body: { error: 'zzq' } }),
      autoUpdateSave: body => (++saves === 1
        ? { status: 200, body: { ok: true, auto_update: body.enabled } }
        : { status: 500, body: { error: 'boom' } }),
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    expect(await screen.findByTestId('auto-update-read-error')).toBeTruthy()

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    await waitFor(() => expect(saves).toBe(1))
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-save-error')).toHaveTextContent('boom')
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
  })

  it("after a failed read, a save's answer replaces the default notice, and a click in flight shows neither", async () => {
    let answerSave: () => void = () => {}
    const spy = stubFetch({}, { config: () => Promise.resolve({ status: 500, body: { error: 'zzq' } }) })
    const route = spy.getMockImplementation()!
    spy.mockImplementation(async (input: unknown, init?: RequestInit) => {
      if (String(input).includes('/api/update/auto')) await new Promise<void>(resolve => { answerSave = resolve })
      return route(input, init)
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    expect(await screen.findByTestId('auto-update-read-error')).toHaveTextContent(
      i18nT('pages.settings.aboutPanel.auto_update_setting_unavailable'))

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    // In flight: the switch shows the click, so "shows the default: on" is false.
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))
    expect(screen.queryByTestId('auto-update-read-error')).toBeNull()
    expect(screen.queryByTestId('auto-update-refresh-error')).toBeNull()

    await act(async () => { answerSave() })
    // The re-read fails again: the switch shows the save's answer, and says so.
    expect(await screen.findByTestId('auto-update-refresh-error')).toHaveTextContent(
      i18nT('pages.settings.aboutPanel.auto_update_save_answer_shown'))
    expect(screen.queryByTestId('auto-update-read-error')).toBeNull()
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false')
  })

  it('says a refresh failed, and keeps the last value read', async () => {
    let fail = false
    stubFetch({}, {
      config: () => Promise.resolve(fail ? { status: 500, body: { error: 'zzq' } } : { status: 200, body: { auto_update: false } }),
    })
    pushStatus({ update_auto_effect: 'install' })
    const { qc } = mountWeb()
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'))

    fail = true
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(await screen.findByTestId('auto-update-refresh-error')).toBeTruthy()
    live()
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false')
  })

  it('reports a write config.local.json refused, and notes the pin until a save says it is gone', async () => {
    let pinned = true
    stubFetch({}, {
      autoUpdateSave: body => pinned
        ? { status: 409, body: { error: 'auto_update is set in config.local.json', code: 'auto_update_overlay_owned', overlay_override: true } }
        : { status: 200, body: { ok: true, auto_update: body.enabled, overlay_override: false } },
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)
    const pinText = i18nT('pages.settings.privacyPanel.recordMetricsOverlayPinned')

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-save-error')).toHaveTextContent(pinText)
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true'))
    // Still usable: a click is how a removed override is noticed.
    live()

    pinned = false
    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    await waitFor(() => expect(screen.queryByTestId('auto-update-save-error')).toBeNull())
    expect(screen.queryByTestId('auto-update-pin-note')).toBeNull()
  })

  it('notes a config.local.json pin a check reports, before any click, and ties it to the switch', async () => {
    stubFetch({ check_status: 'succeeded', overlay_override: true })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)
    expect(screen.queryByTestId('auto-update-pin-note')).toBeNull()

    await pressCheck()
    const pin = await screen.findByTestId('auto-update-pin-note')
    expect(screen.getByRole('switch', { name: LABEL }).closest('[aria-describedby]')?.getAttribute('aria-describedby') ?? '')
      .toContain(pin.id)
  })

  it('notes the pin when a save is accepted but config.local.json still decides', async () => {
    stubFetch({}, {
      autoUpdateSave: () => ({ status: 200, body: { ok: true, auto_update: true, overlay_override: true } }),
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-pin-note')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true'))
  })

  it('reports a failed save until a read made after it shows the write landed, and not again later', async () => {
    // The POST persisted but its answer was lost.
    let saved = true
    let answerRead: (() => void) | null = null
    stubFetch({}, {
      config: () => answerRead === null && saved === false
        ? new Promise(resolve => { answerRead = () => resolve({ status: 200, body: { auto_update: saved } }) })
        : Promise.resolve({ status: 200, body: { auto_update: saved } }),
      autoUpdateSave: body => { saved = body.enabled as boolean; return { status: 502, body: { error: 'zzq lost' } } },
    })
    pushStatus({ update_auto_effect: 'install' })
    const { qc } = mountWeb()
    await waitFor(live)

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-save-error')).toBeTruthy()
    await waitFor(() => expect(answerRead).not.toBeNull())
    await act(async () => { answerRead!() })
    await waitFor(() => expect(screen.queryByTestId('auto-update-save-error')).toBeNull())
    expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false')

    // Another tab or the CLI turns it back on: the cleared failure stays cleared.
    saved = true
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true'))
    expect(screen.queryByTestId('auto-update-save-error')).toBeNull()
  })

  it('keeps a failed re-toggle reported while the cache still holds its value from before', async () => {
    // On, then off (saved), then on again (fails), and the re-reads fail. The
    // cache still reads "on" from before both saves; that read proves nothing
    // about the failed one.
    let reads = 0
    let calls = 0
    stubFetch({}, {
      config: () => Promise.resolve(++reads === 1 ? { status: 200, body: { auto_update: true } } : { status: 500, body: { error: 'zzq down' } }),
      autoUpdateSave: body => (++calls === 1
        ? { status: 200, body: { ok: true, auto_update: body.enabled } }
        : { status: 500, body: { error: 'zzq refused' } }),
    })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-save-error')).toHaveTextContent('zzq refused')
    await waitFor(() => expect(calls).toBe(2))
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(screen.getByTestId('auto-update-save-error')).toHaveTextContent('zzq refused')
  })

  it('keeps reporting a failed read while a retry is in flight', async () => {
    let fail = true
    let release: () => void = () => {}
    stubFetch({}, {
      config: () => fail
        ? Promise.resolve({ status: 500, body: { error: 'zzq' } })
        : new Promise(resolve => { release = () => resolve({ status: 200, body: { auto_update: true } }) }),
    })
    pushStatus({ update_auto_effect: 'install' })
    const { qc } = mountWeb()
    expect(await screen.findByTestId('auto-update-read-error')).toBeTruthy()

    fail = false
    act(() => { void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.fetchStatus).toBe('fetching'))
    expect(screen.getByTestId('auto-update-read-error')).toBeTruthy()
    act(() => release())
    await waitFor(() => expect(screen.queryByTestId('auto-update-read-error')).toBeNull())
  })

  it('reports a refused save that did not land', async () => {
    stubFetch({}, { autoUpdateSave: () => ({ status: 500, body: { error: 'zzq refused' } }) })
    pushStatus({ update_auto_effect: 'install' })
    mountWeb()
    await waitFor(live)

    fireEvent.click(screen.getByRole('switch', { name: LABEL }))
    expect(await screen.findByTestId('auto-update-save-error')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'true'))
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
