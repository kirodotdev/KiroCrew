import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these methods, so mocking them keeps
 * every case network-free. The rest of the module stays real: the 409 parser
 * and `ApiError` are part of what is under test. The install POST resolves with
 * the same shape as the GET, which is what lets the panel re-render from one
 * answer. */
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      getBrowserInstall: vi.fn(),
      installBrowserCli: vi.fn(),
      installBrowserEngine: vi.fn(),
      setBrowserToken: vi.fn(),
      system: vi.fn(),
      dashboardConfig: vi.fn(),
      updateDashboardConfig: vi.fn(),
    },
  }
})

/* `isElectron` is a module constant; a getter lets one case flip it. */
const electron = vi.hoisted(() => ({ value: false }))
vi.mock('../../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/electron')>()),
  get isElectron() {
    return electron.value
  },
}))

import { api, type BrowserInstallJob } from '../../api/client'
import { ApiError } from '../../api/apiError'
// `/all` for the Japanese catalog used by the language-switch case.
import { i18next } from '../../i18n/all'
import { BrowserPanel } from './BrowserPanel'

type State = {
  installed: boolean
  cli_path: string | null
  cli_version: string | null
  node_ok: boolean
  standalone_install?: string
  node_version: string | null
  browser_ok: boolean
  installing: boolean
  last_error: string | null
  token: boolean
  browsers?: Record<string, boolean>
  browser_status?: Record<string, 'downloaded' | 'missing' | 'unknown'>
  install_job?: BrowserInstallJob | null
}

function state(overrides: Partial<State> = {}): State {
  return {
    installed: false,
    cli_path: null,
    cli_version: null,
    node_ok: true,
    standalone_install: 'curl -fsSLO https://example.invalid/playwright-cli.sh && sh playwright-cli.sh',
    node_version: '22.0.0',
    browser_ok: false,
    installing: false,
    last_error: null,
    token: false,
    browsers: { chromium: true, firefox: false, webkit: false },
    install_job: null,
    ...overrides,
  }
}

function job(overrides: Partial<BrowserInstallJob> = {}): BrowserInstallJob {
  return {
    id: 'a1b2c3',
    kind: 'engine_download',
    engine: 'firefox',
    status: 'running',
    stage: 'downloading_browser',
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:30Z',
    finished_at: null,
    elapsed_s: 30,
    error_code: null,
    error_detail: null,
    ...overrides,
  }
}

const installed = (overrides: Partial<State> = {}) =>
  state({ installed: true, cli_version: '0.1.18', browser_ok: true, ...overrides })

const mockGet = () => vi.mocked(api.getBrowserInstall)
const engineButton = (engine: string) =>
  screen.getByTestId(`browser-engine-${engine}`).querySelector('button') as HTMLButtonElement | null

/** The text an element's `aria-describedby` points at, as assistive tech reads it. */
function describedText(el: HTMLElement): string {
  const ids = (el.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
  return ids.map((id) => document.getElementById(id)?.textContent ?? '').join(' ')
}

async function renderPanel(data: State = state(), afterInstall: State = state({ installing: true })) {
  mockGet().mockResolvedValue(data as never)
  vi.mocked(api.installBrowserCli).mockResolvedValue(afterInstall as never)
  vi.mocked(api.installBrowserEngine).mockResolvedValue(afterInstall as never)
  const utils = renderWithProviders(<BrowserPanel />)
  // Wait for the SKELETON to go, not for a title: the loading state renders a
  // title too, so waiting on it would let every assertion run against loading.
  await waitFor(() => expect(document.querySelector('[data-slot="skeleton"]')).toBeNull())
  return utils
}

describe('BrowserPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    electron.value = false
    vi.mocked(api.system).mockResolvedValue({ hostname: 'fedora-box' } as never)
    vi.mocked(api.dashboardConfig).mockResolvedValue({ use_builtin_browser: true } as never)
  })
  afterAll(async () => {
    await i18next.changeLanguage('en')
  })

  it('reports the CLI as installed, separately from any browser download', async () => {
    await renderPanel(installed())
    expect(screen.getByTestId('browser-cli-installed').textContent).toMatch(/Playwright CLI installed/)
    expect(screen.getByText(/0\.1\.18/)).toBeTruthy()
    // Presence-as-consent is disclosed here because no switch carries it.
    expect(screen.getByText(/installed. Removing it/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
    // No blanket "can browse" claim: engine readiness is its own row.
    expect(screen.queryByText(/can browse web pages/i)).toBeNull()
  })

  it('names the three setup paths and the host the downloads land on', async () => {
    await renderPanel(installed())
    expect(screen.getByText(/Managed browsers on the Kiro Crew host/)).toBeTruthy()
    expect(screen.getByText('Connect your existing browser')).toBeTruthy()
    expect(screen.getByText('Built-in browser in the desktop app')).toBeTruthy()
    await waitFor(() => expect(screen.getByTestId('browser-host-label').textContent).toMatch(/fedora-box/))
    expect(screen.getByText(/not on the device showing this page/)).toBeTruthy()
    expect(within(screen.getByTestId('browser-engine-chromium')).getByText('Default for automation')).toBeTruthy()
  })

  it('reports a failed host read instead of silently dropping the host badge', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('system read refused'))
    await renderPanel(installed())
    // Rendered through ErrorNotice (role=alert), so the failure is not a bare string.
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/system read refused/))
    expect(screen.queryByTestId('browser-host-label')).toBeNull()
    // Every section is still there: the host name is a label, not a prerequisite.
    expect(screen.getByTestId('browser-cli-installed')).toBeTruthy()
    expect(screen.getByTestId('browser-engine-firefox')).toBeTruthy()
    expect(screen.getByLabelText(/token/i)).toBeTruthy()
    expect(screen.getByRole('switch', { name: /Use the built-in browser/ })).toBeTruthy()
    // No hand-off: the token draft shares the panel.
    expect(screen.queryByTitle(/open a chat with this error/i)).toBeNull()
  })

  it('offers the install when the CLI is absent', async () => {
    await renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Install Playwright CLI/i }))
    await waitFor(() => expect(api.installBrowserCli).toHaveBeenCalledTimes(1))
  })

  it('reports a too-old Node instead of an install button that would fail', async () => {
    await renderPanel(state({ node_ok: false, node_version: '18.4.0' }))
    expect(screen.getByText(/Needs Node\.js 20 or newer/i)).toBeTruthy()
    expect(screen.getByText(/18\.4\.0/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
  })

  it('keeps CLI setup progress visible after the CLI binary appears mid-setup', async () => {
    const early = state({ installing: true, install_job: job({ kind: 'cli_setup', engine: null, stage: 'installing_cli' }) })
    const later = installed({
      installing: true,
      browser_ok: false,
      browsers: { chromium: false, firefox: false, webkit: false },
      install_job: job({ kind: 'cli_setup', engine: null, stage: 'installing_skills', elapsed_s: 95 }),
    })
    const { queryClient } = await renderPanel(early)
    const region = screen.getByTestId('browser-install-status')
    expect(region.textContent).toMatch(/Setting up the Playwright CLI/)
    expect(region.textContent).toMatch(/Installing the Playwright CLI/)

    mockGet().mockResolvedValue(later as never)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['browserInstall'] }) })
    const after = screen.getByTestId('browser-install-status')
    await waitFor(() => expect(after.textContent).toMatch(/Registering the agent's browser skills/))
    expect(after.textContent).toMatch(/Setting up the Playwright CLI/)
    expect(after.textContent).not.toMatch(/finished/)
    expect(screen.getByTestId('browser-install-elapsed').textContent).toMatch(/Running for/)
    // The binary is on disk, but the row follows the job: it does not say
    // "installed" or promise browsing while the status region says "setting up".
    expect(screen.queryByTestId('browser-cli-installed')).toBeNull()
    expect(screen.queryByText(/Playwright CLI installed/)).toBeNull()
    expect(screen.queryByText(/Browsing is available/)).toBeNull()
    const cliButton = screen.getByRole('button', { name: /Installing/ })
    expect(cliButton.getAttribute('aria-busy')).toBe('true')
    expect((cliButton as HTMLButtonElement).disabled).toBe(true)
    // Engine downloads wait for the setup, and say so.
    expect(engineButton('firefox')!.disabled).toBe(true)
    expect(describedText(engineButton('firefox')!)).toMatch(/Playwright CLI is being set up/)
    // ONE copy of the sentence, under the Installing button, describing both the
    // Install control and the engine rows; not a second copy under the list.
    expect(screen.getAllByText(/Playwright CLI is being set up/)).toHaveLength(1)
    expect(screen.queryByTestId('browser-engine-reason')).toBeNull()
    const reasonIds = (el: HTMLElement) => (el.getAttribute('aria-describedby') ?? '').split(/\s+/)
    expect(describedText(cliButton)).toMatch(/Playwright CLI is being set up/)
    expect(reasonIds(engineButton('firefox')!)).toEqual(reasonIds(cliButton))
    expect(reasonIds(engineButton('webkit')!)).toEqual(reasonIds(cliButton))
  })

  it('claims the CLI is installed once the setup job has finished', async () => {
    await renderPanel(
      installed({
        install_job: job({ kind: 'cli_setup', engine: null, status: 'succeeded', stage: 'finishing', finished_at: '2026-01-01T00:02:00Z', elapsed_s: 120 }),
      }),
    )
    expect(screen.getByTestId('browser-cli-installed').textContent).toMatch(/Playwright CLI installed/)
    expect(screen.queryByRole('button', { name: /Installing/ })).toBeNull()
  })

  it('identifies a Firefox download started elsewhere (refresh or second tab)', async () => {
    // A fresh mount has no mutation state at all: attribution must come from the job.
    await renderPanel(installed({ installing: true, install_job: job() }))
    expect(screen.getByTestId('browser-install-status').textContent).toMatch(/Downloading Firefox/)
    expect(screen.getByTestId('browser-engine-firefox').getAttribute('data-state')).toBe('downloading')
    expect(screen.getByTestId('browser-engine-webkit').getAttribute('data-state')).toBe('missing')
    const webkit = engineButton('webkit')!
    expect(webkit.disabled).toBe(true)
    expect(describedText(webkit)).toBe('Firefox is downloading. Other downloads will be available when it finishes.')
    expect(screen.getByTestId('browser-engine-reason').textContent).toMatch(/Firefox is downloading/)
  })

  it('blocks duplicate clicks while the POST is pending, attributing only the pressed engine', async () => {
    await renderPanel(installed())
    // Hold the mutation open: the pending window is the whole subject.
    vi.mocked(api.installBrowserEngine).mockReturnValue(new Promise(() => {}))
    fireEvent.click(engineButton('firefox')!)
    await waitFor(() => expect(engineButton('webkit')!.disabled).toBe(true))
    expect(screen.getByTestId('browser-engine-firefox').getAttribute('data-state')).toBe('downloading')
    expect(screen.getByTestId('browser-engine-webkit').getAttribute('data-state')).toBe('missing')
    expect(screen.getByTestId('browser-install-status').textContent).toMatch(/Downloading Firefox/)
    fireEvent.click(engineButton('webkit')!)
    expect(api.installBrowserEngine).toHaveBeenCalledTimes(1)
  })

  it('shows the active job a 409 names instead of the refused request', async () => {
    await renderPanel(installed())
    const active = job({ engine: 'webkit' })
    vi.mocked(api.installBrowserEngine).mockRejectedValue(
      new ApiError(409, 'A browser install is already running.', JSON.stringify({
        error: 'A browser install is already running.',
        code: 'install_already_running',
        install_job: active,
      })),
    )
    // Later reads agree with the conflict, as the gateway would.
    mockGet().mockResolvedValue(installed({ installing: true, install_job: active }) as never)
    fireEvent.click(engineButton('firefox')!)
    await waitFor(() =>
      expect(screen.getByTestId('browser-engine-webkit').getAttribute('data-state')).toBe('downloading'),
    )
    expect(screen.getByTestId('browser-engine-firefox').getAttribute('data-state')).toBe('missing')
    expect(screen.getByTestId('browser-install-status').textContent).toMatch(/Downloading WebKit/)
    expect(screen.queryByText(/already running/)).toBeNull()
  })

  it('reports a 409 that carries no job as a refused request', async () => {
    await renderPanel(installed())
    vi.mocked(api.installBrowserEngine).mockRejectedValue(
      new ApiError(409, 'A browser install is already running.', '{"error": "busy"}'),
    )
    fireEvent.click(engineButton('firefox')!)
    await waitFor(() => expect(screen.getByText(/already running/)).toBeTruthy())
  })

  it('re-reads the status before allowing a retry when the POST answer is lost', async () => {
    await renderPanel(installed())
    let answer: (v: unknown) => void = () => {}
    vi.mocked(api.installBrowserEngine).mockRejectedValue(new TypeError('Failed to fetch'))
    mockGet().mockReturnValue(new Promise((resolve) => { answer = resolve }) as never)
    fireEvent.click(engineButton('firefox')!)
    await waitFor(() => expect(describedText(engineButton('webkit')!)).toMatch(/Checking whether the last request/))
    expect(engineButton('firefox')!.disabled).toBe(true)

    // The status answer says the download did start; the row follows it.
    await act(async () => { answer(installed({ installing: true, install_job: job() })) })
    await waitFor(() =>
      expect(screen.getByTestId('browser-engine-firefox').getAttribute('data-state')).toBe('downloading'),
    )
    expect(describedText(engineButton('webkit')!)).toMatch(/Firefox is downloading/)
  })

  it('renders a failed first status read as an error', async () => {
    mockGet().mockRejectedValue(new Error('boom'))
    renderWithProviders(<BrowserPanel />)
    await waitFor(() => expect(screen.getByText(/Cannot load browser status/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Download/ })).toBeNull()
  })

  it('marks a failed refetch as an error and pauses installs, keeping the token draft', async () => {
    const { queryClient } = await renderPanel(installed())
    const field = screen.getByLabelText(/token/i) as HTMLInputElement
    fireEvent.change(field, { target: { value: 'draft-token-123' } })

    mockGet().mockRejectedValue(new Error('gateway unreachable'))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['browserInstall'] }) })
    await waitFor(() => expect(screen.getByText(/Cannot read the browser setup status/)).toBeTruthy())
    expect(engineButton('firefox')!.disabled).toBe(true)
    expect(describedText(engineButton('firefox')!)).toMatch(/paused until the setup status loads/)
    // The draft is still there, never copied into the error, and no hand-off is
    // offered that would navigate away from it.
    expect((screen.getByLabelText(/token/i) as HTMLInputElement).value).toBe('draft-token-123')
    expect(screen.getByTestId('browser-install-status').textContent).not.toMatch(/draft-token-123/)
    expect(screen.queryByTitle(/open a chat with this error/i)).toBeNull()
  })

  it('renders an older gateway honestly: generic busy copy and Unknown engines', async () => {
    const old = installed({ installing: true, browsers: undefined })
    delete old.install_job
    delete old.browser_status
    await renderPanel(old)
    expect(screen.getByTestId('browser-install-status').textContent).toMatch(
      /Browser setup is running on the .+ host\./,
    )
    // The headline and the row reason are different strings, so the screen does
    // not print the same sentence twice.
    expect(screen.getByTestId('browser-install-status').textContent).not.toMatch(
      /Another browser setup operation is running\./,
    )
    for (const engine of ['chromium', 'firefox', 'webkit']) {
      expect(screen.getByTestId(`browser-engine-${engine}`).getAttribute('data-state')).toBe('unknown')
    }
    expect(screen.getAllByText('Unknown')).toHaveLength(3)
    // The row's own reason, then the shared block reason.
    expect(describedText(engineButton('webkit')!)).toBe(
      'Kiro Crew could not confirm this build from the browser cache. Downloading again is safe. Another browser setup operation is running.',
    )
    expect(document.body.textContent).not.toMatch(/undefined/)
  })

  it('says why an engine is Unknown once, below the list, next to its still-enabled Download', async () => {
    await renderPanel(installed({ browser_status: { chromium: 'unknown', firefox: 'unknown', webkit: 'missing' } }))
    const reason = screen.getByTestId('browser-engine-unknown-reason')
    expect(reason.textContent).toBe(
      'Kiro Crew could not confirm this build from the browser cache. Downloading again is safe.',
    )
    // Two unknown rows, one sentence: each row's button points at the same element.
    expect(screen.getAllByText(/could not confirm this build/)).toHaveLength(1)
    expect(screen.getAllByText('Unknown')).toHaveLength(2)
    // Not an error: nothing failed, so it is plain status text, not an alert.
    expect(screen.queryByRole('alert')).toBeNull()
    for (const engine of ['chromium', 'firefox']) {
      const button = engineButton(engine)!
      expect(button.disabled).toBe(false)
      expect(button.getAttribute('aria-describedby')).toBe(reason.id)
      expect(describedText(button)).toBe(reason.textContent)
    }
    // Only the unknown rows are described by it.
    expect(engineButton('webkit')!.getAttribute('aria-describedby')).toBeNull()
  })

  it('renders no unknown reason when no engine is unknown', async () => {
    await renderPanel(installed({ browser_status: { chromium: 'downloaded', firefox: 'missing', webkit: 'missing' } }))
    expect(screen.queryByTestId('browser-engine-unknown-reason')).toBeNull()
    expect(engineButton('firefox')!.getAttribute('aria-describedby')).toBeNull()
  })

  it('prefers the explicit unknown engine status over the boolean', async () => {
    await renderPanel(installed({ browser_status: { chromium: 'downloaded', firefox: 'unknown', webkit: 'missing' } }))
    expect(screen.getByTestId('browser-engine-firefox').getAttribute('data-state')).toBe('unknown')
    expect(screen.getByTestId('browser-engine-webkit').getAttribute('data-state')).toBe('missing')
  })

  it('shows a finished failure with its detail and offers Retry on that engine', async () => {
    await renderPanel(
      installed({
        install_job: job({
          status: 'failed',
          stage: 'downloading_browser',
          finished_at: '2026-01-01T00:01:00Z',
          elapsed_s: 60,
          error_code: 'step_failed',
          error_detail: 'install-browser firefox: missing libgtk-3',
        }),
        last_error: 'install-browser firefox: missing libgtk-3',
      }),
    )
    const region = screen.getByTestId('browser-install-status')
    expect(region.textContent).toMatch(/Firefox download failed/)
    expect(region.textContent).toMatch(/missing libgtk-3/)
    expect(within(screen.getByTestId('browser-engine-firefox')).getByRole('button', { name: /Retry/ })).toBeTruthy()
    // No token draft, so the hand-off is safe to offer.
    expect(screen.getByTitle(/open a chat with this error/i)).toBeTruthy()
  })

  it('reports a finished success without claiming the browser was verified', async () => {
    await renderPanel(
      installed({
        browsers: { chromium: true, firefox: true, webkit: false },
        install_job: job({ status: 'succeeded', stage: 'finishing', finished_at: '2026-01-01T00:01:00Z', elapsed_s: 65 }),
      }),
    )
    const region = screen.getByTestId('browser-install-status')
    expect(region.textContent).toMatch(/Firefox download finished/)
    expect(region.textContent).toMatch(/Took/)
    expect(within(screen.getByTestId('browser-engine-firefox')).getByText('Downloaded')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/verified/i)
  })

  it('surfaces a failed install from an older gateway verbatim', async () => {
    const old = state({ last_error: 'npm: E401 Unable to authenticate' })
    delete old.install_job
    await renderPanel(old)
    expect(screen.getByText(/E401 Unable to authenticate/)).toBeTruthy()
    expect(screen.getByTitle(/open a chat with this error/i)).toBeTruthy()
  })

  it('explains the desktop-only toggle on the web, readable and linked to the switch', async () => {
    await renderPanel(installed())
    const toggle = screen.getByRole('switch', { name: /Use the built-in browser/ })
    expect(toggle.getAttribute('aria-disabled')).toBe('true')
    expect(describedText(toggle)).toMatch(/Available in the Kiro Crew desktop app/)
  })

  it('offers the toggle in the desktop app, independent of the CLI state', async () => {
    electron.value = true
    await renderPanel(state({ installed: false }))
    const toggle = screen.getByRole('switch', { name: /Use the built-in browser/ })
    await waitFor(() => expect(toggle.getAttribute('aria-disabled')).toBeNull())
    expect(screen.getByText(/built-in panel/)).toBeTruthy()
    expect(screen.queryByText(/Available in the Kiro Crew desktop app/)).toBeNull()
  })

  it('keeps a token draft through polling, a save error and a language switch', async () => {
    const { queryClient, rerender } = await renderPanel(installed())
    const field = () => screen.getByLabelText(/token|トークン/i) as HTMLInputElement
    fireEvent.change(field(), { target: { value: 'secret-draft-value' } })

    // A poll answer re-renders every section around the field.
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['browserInstall'] }) })
    expect(field().value).toBe('secret-draft-value')

    // A rejected save leaves the draft in place and does not echo it.
    vi.mocked(api.setBrowserToken).mockRejectedValue(new Error('token store unavailable'))
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))
    await waitFor(() => expect(screen.getByText(/token store unavailable/)).toBeTruthy())
    expect(field().value).toBe('secret-draft-value')
    for (const alert of screen.getAllByRole('alert')) {
      expect(alert.textContent).not.toMatch(/secret-draft-value/)
    }

    // A language switch re-renders in place (as LanguageProvider does), never remounts.
    await act(async () => { await i18next.changeLanguage('ja') })
    rerender(<BrowserPanel />)
    await waitFor(() => expect(screen.getByText('既存のブラウザーに接続')).toBeTruthy())
    expect(field().value).toBe('secret-draft-value')
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('names the remedy when Node is missing entirely, not just the requirement', async () => {
    await renderPanel(state({ node_ok: false, node_version: null }))
    expect(screen.getByText(/no Node was found/i)).toBeTruthy()
    expect(screen.getByRole('link', { name: /Download Node\.js/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
  })

  it('offers the no-admin installer, so a locked-down machine is not a dead end', async () => {
    await renderPanel(state({ node_ok: false, node_version: null }))
    expect(screen.getByText(/no admin rights/i)).toBeTruthy()
    // ONE command, composed by the gateway: it knows its own OS, and this page
    // may be open on a different machine than the one being installed onto.
    expect(screen.getByText(/playwright-cli\.sh/)).toBeTruthy()
  })

  it('offers a copy button for the command, which must be exact', async () => {
    await renderPanel(state({ node_ok: false, node_version: null }))
    expect(screen.getByRole('button', { name: /Copy command/i })).toBeTruthy()
  })

  it('saves an attach token the operator types, and clears the field after', async () => {
    const setToken = vi.mocked(api.setBrowserToken)
    setToken.mockResolvedValue({ ok: true, token: true })
    await renderPanel(installed())
    const field = screen.getByLabelText(/token/i)
    fireEvent.change(field, { target: { value: 'attach-token-value' } })
    expect((field as HTMLInputElement).value).toBe('attach-token-value')
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))
    await waitFor(() => expect(setToken).toHaveBeenCalledWith('attach-token-value'))
    await waitFor(() => expect((field as HTMLInputElement).value).toBe(''))
  })

  it('saves the typed token on Enter, and ignores Enter on an empty field', async () => {
    const setToken = vi.mocked(api.setBrowserToken)
    setToken.mockResolvedValue({ ok: true, token: true })
    await renderPanel(installed())
    const field = screen.getByLabelText(/token/i)
    fireEvent.keyDown(field, { key: 'Enter', code: 'Enter' })
    expect(setToken).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: 'enter-token-value' } })
    fireEvent.keyDown(field, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(setToken).toHaveBeenCalledWith('enter-token-value'))
  })

  it('clears a stored token with an empty save', async () => {
    const setToken = vi.mocked(api.setBrowserToken)
    setToken.mockResolvedValue({ ok: true, token: false })
    await renderPanel(installed({ token: true }))
    expect(screen.getByText('saved')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Clear$/ }))
    await waitFor(() => expect(setToken).toHaveBeenCalledWith(''))
  })

  it('keeps the token field available while the CLI is not installed', async () => {
    await renderPanel(state({ installed: false }))
    expect(screen.getByLabelText(/token/i)).toBeTruthy()
  })

  it('says nothing extra when the gateway is too old to offer a command', async () => {
    await renderPanel(state({ node_ok: false, node_version: null, standalone_install: undefined }))
    expect(screen.getByText(/no Node was found/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Copy command/i })).toBeNull()
    expect(screen.queryByText(/no admin rights/i)).toBeNull()
  })

  it('confirms the copy only after the clipboard write resolves', async () => {
    const orig = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
    try {
      await renderPanel(state({ node_ok: false, node_version: null }))
      fireEvent.click(screen.getByRole('button', { name: /Copy command/i }))
      await waitFor(() => expect(screen.getByRole('button', { name: /Copied/i })).toBeTruthy())
      expect(screen.queryByRole('alert')).toBeNull()
    } finally {
      if (orig) Object.defineProperty(navigator, 'clipboard', orig)
      else delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('reports a failed copy instead of painting "Copied"', async () => {
    const origClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const origExec = document.execCommand
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    document.execCommand = vi.fn().mockReturnValue(false)
    try {
      await renderPanel(state({ node_ok: false, node_version: null }))
      fireEvent.click(screen.getByRole('button', { name: /Copy command/i }))
      await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
      expect(screen.queryByRole('button', { name: /Copied/i })).toBeNull()
      expect(screen.getByRole('button', { name: /Copy command/i })).toBeTruthy()
    } finally {
      document.execCommand = origExec
      if (origClipboard) Object.defineProperty(navigator, 'clipboard', origClipboard)
      else delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }
  })

  it('does not push the installer at someone whose Node is fine', async () => {
    await renderPanel(state({ node_ok: true, installed: false }))
    expect(screen.queryByText(/no admin rights/i)).toBeNull()
    expect(screen.queryByText(/playwright-cli\.sh/)).toBeNull()
  })

  it('offers every engine as its own download', async () => {
    await renderPanel(installed())
    expect(screen.getByTestId('browser-engine-chromium')).toBeTruthy()
    expect(screen.getByTestId('browser-engine-firefox')).toBeTruthy()
    expect(screen.getByTestId('browser-engine-webkit')).toBeTruthy()
    // A downloaded engine shows state, not a button; a missing one is actionable.
    expect(engineButton('chromium')).toBeNull()
    fireEvent.click(engineButton('firefox')!)
    await waitFor(() => expect(api.installBrowserEngine).toHaveBeenCalledWith('firefox'))
  })

  it('asks for the CLI before offering engine downloads', async () => {
    await renderPanel(state({ installed: false }))
    expect(engineButton('firefox')!.disabled).toBe(true)
    expect(describedText(engineButton('firefox')!)).toBe('Install the Playwright CLI first.')
  })
})
