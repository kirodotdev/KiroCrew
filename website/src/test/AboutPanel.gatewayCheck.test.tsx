//
// Contract under test — the gateway (non-Electron) "Check for updates" flow.
//
// The bug: the success line was rendered on `gwCheck.isSuccess && !showUpdate`,
// i.e. on any HTTP 200. For a wheel install the backend check never actually ran,
// so a check that did nothing told the user they were up to date while two
// releases behind. `checked` is now the verdict and a 200 is only transport.
//
// - checked:false + an error code  -> failure line, and NEVER the success line
// - an UNRECOGNISED error code     -> generic reason, still not the success line
// - checked:true + available:false -> the success line (the only case that earns it)
// - available + !self_updatable    -> the installer command, and NO Update button
// - available + self_updatable     -> the Update button, unchanged
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
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
    stubFetch({ checked: false, available: false, error: 'feed_unreachable', install_kind: 'wheel' })
    mountWeb()
    await pressCheck()

    const failed = await screen.findByTestId('check-failed')
    expect(failed.textContent).toContain("Couldn't check for updates")
    expect(failed.textContent).toContain('release feed')
    expect(screen.queryByTestId('up-to-date')).toBeNull()
  })

  it('an unrecognised error code falls back to the generic reason', async () => {
    // A newer gateway paired with this bundle must still say the check failed.
    stubFetch({ checked: false, available: false, error: 'some_future_code' })
    mountWeb()
    await pressCheck()

    const failed = await screen.findByTestId('check-failed')
    expect(failed.textContent).toContain("The check didn't complete")
    expect(screen.queryByTestId('up-to-date')).toBeNull()
  })

  it('reports up to date only when a comparison actually completed', async () => {
    stubFetch({ checked: true, available: false, error: '', install_kind: 'wheel' })
    mountWeb()
    await pressCheck()

    const ok = await screen.findByTestId('up-to-date')
    expect(ok.textContent).toContain('latest version')
    expect(screen.queryByTestId('check-failed')).toBeNull()
  })

  it('offers the installer command instead of a broken Update button on a wheel install', async () => {
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider"
    stubFetch({
      checked: true,
      available: true,
      error: '',
      install_kind: 'wheel',
      self_updatable: false,
      channel: 'insider',
      remote_version: '0.1.3rc2',
      update_command: command,
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

  it('copying the command flips the button label', async () => {
    const command = "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel stable"
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    stubFetch({
      checked: true,
      available: true,
      install_kind: 'wheel',
      self_updatable: false,
      channel: 'stable',
      update_command: command,
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
    stubFetch({ checked: false, available: false, error: code, install_kind: 'dmg' })
    mountWeb()
    await pressCheck()

    const note = await screen.findByTestId('check-not-applicable')
    expect(note.textContent).toContain(phrase)
    expect(screen.queryByTestId('up-to-date')).toBeNull()
    expect(screen.queryByTestId('check-failed')).toBeNull()
  })

  it('a git checkout still gets the Update button', async () => {
    stubFetch({
      checked: true,
      available: true,
      error: '',
      install_kind: 'git',
      self_updatable: true,
      remote_version: '0.1.3',
      changes: '### 0.1.3\n- thing',
    })
    mountWeb()
    await pressCheck()

    await waitFor(() => expect(screen.getByRole('button', { name: /^Update/ })).toBeTruthy())
    expect(screen.queryByTestId('manual-update-instructions')).toBeNull()
  })

  it('names the target version from remote_version', async () => {
    // The panel used to read `d.version`, which the gateway never emits — so the
    // "(vX)" suffix silently never appeared for a gateway install.
    stubFetch({
      checked: true,
      available: true,
      install_kind: 'git',
      self_updatable: true,
      remote_version: '0.1.3rc2',
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
      update_self_updatable: false,
      update_checked: true,
      update_command: command,
    })
    mountWeb()

    const block = await screen.findByTestId('manual-update-command')
    expect(block.textContent).toBe(command)
    expect(screen.queryByRole('button', { name: /^Update/ })).toBeNull()
  })

  it('suppresses the Update button even when no command is known', async () => {
    // Fail safe: `!self_updatable` alone must disarm the button. A gateway that
    // predates `update_command` still must not offer a POST that answers 409.
    stubFetch({})
    pushStatus({ update_available: true, update_self_updatable: false, update_checked: true })
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
    stubFetch({ checked: true, available: false, error: '' })
    mountWeb()
    await pressCheck()
    await waitFor(() => expect(screen.getByTestId('hero-up-to-date')).toBeTruthy())
    expect(screen.queryByTestId('hero-not-checked')).toBeNull()
  })

  it('a failed check does NOT turn the hero pill green', async () => {
    stubFetch({ checked: false, available: false, error: 'feed_unreachable' })
    mountWeb()
    await pressCheck()
    await screen.findByTestId('check-failed')
    expect(screen.queryByTestId('hero-up-to-date')).toBeNull()
    expect(screen.getByTestId('hero-not-checked')).toBeTruthy()
  })

  it('the auto-apply toggle is reworded where the gateway cannot self-apply', async () => {
    stubFetch({})
    pushStatus({ update_self_updatable: false, update_checked: true })
    mountWeb()

    await waitFor(() =>
      expect(screen.getByText(/Notify when an update is available/)).toBeTruthy(),
    )
    // The auto-apply promise must not be shown where the backend downgrades it.
    expect(screen.queryByText(/Auto-update on restart/)).toBeNull()
  })

  it('the auto-apply toggle keeps its promise on a git checkout', async () => {
    stubFetch({})
    pushStatus({ update_self_updatable: true, update_checked: true })
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
      update_self_updatable: false,
      update_checked: true,
      update_command: command,
    })
    mountWeb()

    fireEvent.click(await screen.findByRole('button', { name: /copy command/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /copied/i })).toBeTruthy())
  })
})
