import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these methods, so mocking them keeps
 * every case network-free. The config read answers the `browser` section and the
 * Preview origins; every write is the generic single-path PATCH. */
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      getBrowserStatus: vi.fn(),
      installBrowserHarness: vi.fn(),
      testBrowser: vi.fn(),
      disconnectBrowser: vi.fn(),
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
      dashboardConfig: vi.fn(),
    },
  }
})

import { api, type BrowserStatusData } from '../../api/client'
import { i18next } from '../../i18n/all'
import { BrowserPanel } from './BrowserPanel'
import { choiceFromHeadless, choiceFromPicker, pickerValueOf, primaryState } from './browserStatus'

function status(overrides: Partial<BrowserStatusData> = {}): BrowserStatusData {
  return {
    installed: true,
    version: '0.1.13',
    browser: { kind: 'user_chrome', headless: false },
    connection: 'connected',
    detail: null,
    chromium: { downloaded: true, version: '156' },
    install_job: null,
    platform: 'darwin',
    ...overrides,
  }
}

type Cfg = {
  browser?: Record<string, unknown>
  dashboard?: { browser_local_origins?: string[] }
}

function config(overrides: Cfg = {}): Cfg {
  return {
    browser: { choice: 'auto', live: { enabled: true, quality: 'auto', max_fps: 60, mask_passwords: true } },
    dashboard: { browser_local_origins: ['localhost:5173'] },
    ...overrides,
  }
}

const mocked = api as unknown as {
  getBrowserStatus: ReturnType<typeof vi.fn>
  installBrowserHarness: ReturnType<typeof vi.fn>
  testBrowser: ReturnType<typeof vi.fn>
  disconnectBrowser: ReturnType<typeof vi.fn>
  kirocrewConfig: ReturnType<typeof vi.fn>
  patchConfig: ReturnType<typeof vi.fn>
  dashboardConfig: ReturnType<typeof vi.fn>
}

beforeEach(async () => {
  await i18next.changeLanguage('en')
  mocked.getBrowserStatus.mockReset().mockResolvedValue(status())
  mocked.installBrowserHarness.mockReset().mockResolvedValue(status({ install_job: { status: 'running', stage: 'venv' } }))
  mocked.testBrowser.mockReset().mockResolvedValue({ ok: true, connection: 'connected', detail: null })
  mocked.disconnectBrowser.mockReset().mockResolvedValue(status({ connection: 'disconnected', browser: null }))
  mocked.kirocrewConfig.mockReset().mockResolvedValue(config())
  mocked.patchConfig.mockReset().mockResolvedValue({ ok: true })
  mocked.dashboardConfig.mockReset().mockResolvedValue({ browse_enabled: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('primaryState', () => {
  it('reports an install job first, whatever else the status says', () => {
    expect(primaryState(status({ install_job: { status: 'running', stage: 'venv' } }))).toBe('installing')
    expect(primaryState(status(), true)).toBe('installing')
  })

  it('splits "not installed" into failed and idle', () => {
    expect(primaryState(status({ installed: false, install_job: { status: 'failed', error_detail: 'x' } }))).toBe('install_failed')
    expect(primaryState(status({ installed: false, install_job: null }))).toBe('not_installed')
  })

  it('leaves a failed job on an INSTALLED engine to the Engine row: the card follows the connection', () => {
    expect(primaryState(status({ install_job: { status: 'failed', error_detail: 'x' } }))).toBe('connected')
    for (const c of ['waiting_allow', 'error', 'disconnected', 'connected'] as const) {
      expect(primaryState(status({ connection: c }))).toBe(c)
    }
  })
})

describe('browser.choice across the picker and the no-window switch', () => {
  it('the picker reads headless as Kiro’s Chromium and an unknown value as auto', () => {
    expect(pickerValueOf('headless')).toBe('kiro_chromium')
    expect(pickerValueOf('kiro_chromium')).toBe('kiro_chromium')
    expect(pickerValueOf('user_chrome')).toBe('user_chrome')
    expect(pickerValueOf('nonsense')).toBe('auto')
  })

  it('picking Kiro’s Chromium while headless writes nothing; any other pick writes that value', () => {
    expect(choiceFromPicker('headless', 'kiro_chromium')).toBeNull()
    expect(choiceFromPicker('auto', 'auto')).toBeNull()
    expect(choiceFromPicker('headless', 'auto')).toBe('auto')
    expect(choiceFromPicker('auto', 'kiro_chromium')).toBe('kiro_chromium')
  })

  it('the switch writes headless on and Kiro’s Chromium (with a window) off', () => {
    expect(choiceFromHeadless(true)).toBe('headless')
    expect(choiceFromHeadless(false)).toBe('kiro_chromium')
  })
})

async function openMore() {
  fireEvent.click(await screen.findByRole('button', { name: 'More' }))
}

describe('BrowserPanel - main card', () => {
  it('connected: the browser, a status line, and Disconnect as the ONE button', async () => {
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-state')).toBe('connected')
    expect(within(card).getByText('Your Chrome')).toBeTruthy()
    expect(within(card).getByTestId('browser-status-line').textContent).toBe('Connected · uses your logins')
    const buttons = within(card).getAllByRole('button').filter(b => b.getAttribute('aria-label') !== 'More information')
    expect(buttons).toHaveLength(1)
    expect(buttons[0].textContent).toContain('Disconnect')
    fireEvent.click(buttons[0])
    await waitFor(() => expect(mocked.disconnectBrowser).toHaveBeenCalledTimes(1))
  })

  it('installing: a disabled spinner button and a progress bar, whoever started the job', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false, version: null, connection: 'disconnected', browser: null, chromium: null, install_job: { status: 'running', stage: 'chromium' } }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-state')).toBe('installing')
    expect(within(card).getByText('Getting the browser ready')).toBeTruthy()
    expect(within(card).getByTestId('browser-primary-action')).toBeDisabled()
    expect(within(card).getByTestId('browser-install-progress').getAttribute('aria-valuetext')).toBe('chromium')
    expect(within(card).getByTestId('browser-status-line').textContent).toBe('Installing. You can chat meanwhile.')
  })

  it('waiting for Allow: yellow, and the button asks the gateway to attach again', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'waiting_allow' }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.className).toContain('border-warn')
    expect(within(card).getByText('Chrome is asking. Click Allow there.')).toBeTruthy()
    fireEvent.click(within(card).getByTestId('browser-primary-action'))
    await waitFor(() => expect(mocked.testBrowser).toHaveBeenCalledTimes(1))
  })

  it('connection error: red, the gateway detail verbatim in an ErrorNotice, Retry re-attaches', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'error', detail: 'CDP handshake refused (ECONNRESET)' }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.className).toContain('border-danger')
    expect(within(card).getByTestId('browser-status-line').textContent).toBe('Connection failed')
    const notice = within(card).getByTestId('browser-status-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('CDP handshake refused (ECONNRESET)')
    expect(within(notice).getByText('Ask the agent')).toBeTruthy()
    const retry = within(card).getByTestId('browser-primary-action')
    expect(retry.textContent).toContain('Retry')
    fireEvent.click(retry)
    await waitFor(() => expect(mocked.testBrowser).toHaveBeenCalledTimes(1))
  })

  it('install failed: red, the job’s own detail, Retry runs the install again', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false, version: null, connection: 'disconnected', browser: null, install_job: { status: 'failed', error_detail: 'downloading: timed out' } }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-state')).toBe('install_failed')
    expect(within(card).getByText('Browser setup failed')).toBeTruthy()
    expect(within(card).queryByTestId('browser-status-line')).toBeNull()
    const notice = within(card).getByTestId('browser-status-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('downloading: timed out')
    expect(within(notice).getByText('Ask the agent')).toBeTruthy()
    fireEvent.click(within(card).getByTestId('browser-primary-action'))
    await waitFor(() => expect(mocked.installBrowserHarness).toHaveBeenCalledWith({}))
  })

  it('an interrupted install on an uninstalled engine reads as failed, with its detail', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false, version: null, connection: 'disconnected', browser: null, install_job: { status: 'interrupted', error_detail: 'interrupted: the gateway stopped this install' } }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-state')).toBe('install_failed')
    expect(within(card).getByTestId('browser-status-error').textContent).toContain('the gateway stopped this install')
  })

  it('a connect in flight is disabled and aria-busy', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    mocked.testBrowser.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    const btn = within(card).getByTestId('browser-primary-action')
    expect(btn).not.toHaveAttribute('aria-busy')
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toHaveAttribute('aria-busy', 'true'))
    expect(btn).toBeDisabled()
  })

  it('the no-window switch does not anchor browser.choice: only the picker does', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    const anchors = document.querySelectorAll('[data-setting-key="browser.choice"]')
    expect(anchors).toHaveLength(1)
    expect(anchors[0].getAttribute('data-setting-label')).toBe('Which browser')
  })

  it('connection error with no status detail shows the attach’s own ok:false detail', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'error', detail: null }))
    mocked.testBrowser.mockResolvedValue({ ok: false, connection: 'error', detail: 'port 9222 closed' })
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(within(card).queryByTestId('browser-status-error')).toBeNull()
    fireEvent.click(within(card).getByTestId('browser-primary-action'))
    const notice = await within(card).findByTestId('browser-status-error')
    expect(notice.textContent).toContain('port 9222 closed')
    expect(screen.queryByTestId('browser-test-result')).toBeNull()
  })

  it('a rejected save shows while More is closed', async () => {
    mocked.patchConfig.mockRejectedValue(new Error('config write refused'))
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    fireEvent.click(screen.getByRole('switch', { name: 'Show the live view in chat' }))
    const notice = await screen.findByTestId('browser-save-error')
    expect(notice.textContent).toContain('config write refused')
  })

  it('disconnected: Connect attaches, and a 200 ok:false is an ErrorNotice with the gateway detail', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    mocked.testBrowser.mockResolvedValue({ ok: false, connection: 'disconnected', detail: 'chrome refused the connection' })
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    fireEvent.click(within(card).getByTestId('browser-primary-action'))
    await waitFor(() => expect(mocked.testBrowser).toHaveBeenCalledTimes(1))
    const notice = await screen.findByTestId('browser-test-result')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('chrome refused the connection')
  })

  it('the explanation is an info tip on the title, not a line on the card', async () => {
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(within(card).queryByText(/Kiro only works in tabs it opened itself/)).toBeNull()
    const tip = within(card).getByRole('button', { name: 'More information' })
    expect(tip.getAttribute('title')).toMatch(/Kiro only works in tabs it opened itself/)
    fireEvent.focus(tip)
    const bubble = await screen.findByRole('tooltip')
    expect(tip.getAttribute('aria-describedby')).toBe(bubble.id)
  })

  it('the removed pieces stay removed: no step list, no fallback chips, no Test, no Mask switch', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    expect(screen.queryByTestId('browser-first-run')).toBeNull()
    expect(screen.queryByTestId('browser-chip-user_chrome')).toBeNull()
    expect(screen.queryByTestId('browser-test')).toBeNull()
    expect(screen.queryByRole('switch', { name: /Mask password/ })).toBeNull()
    expect(screen.getByTestId('browser-privacy-footer').textContent).toContain('Nothing is uploaded')
  })
})

describe('BrowserPanel - controls and More', () => {
  it('shows two controls up front and nothing of More until it opens', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    expect(document.querySelector('[data-setting-key="browser.choice"]')).not.toBeNull()
    expect(screen.getByRole('switch', { name: 'Show the live view in chat' })).toBeTruthy()
    const more = screen.getByRole('button', { name: 'More' })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(document.querySelector('[data-setting-key="browser.live.quality"]')).toBeNull()
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    for (const key of ['browser.live.quality', 'browser.live.max_fps', 'dashboard.browser_local_origins']) {
      expect(document.querySelector(`[data-setting-key="${key}"]`), key).not.toBeNull()
    }
    expect(screen.getByTestId('browser-engine-versions').textContent).toBe('Harness 0.1.13 · Chromium 156')
    expect(screen.getByRole('switch', { name: 'Run with no window' })).toBeTruthy()
  })

  it('every control writes ONE config path; no explanation renders inline', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    fireEvent.click(screen.getByRole('switch', { name: 'Show the live view in chat' }))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.live.enabled', false))
    await openMore()
    fireEvent.click(screen.getByRole('switch', { name: 'Run with no window' }))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.choice', 'headless'))
    // Descriptions are tips (on `title` while closed), never body text.
    for (const text of [/When Kiro uses the browser, the turn shows a live card/, /Auto adapts frame rate/, /60 is the most this machine streams/, /For previewing your own dev server only/]) {
      expect(screen.queryByText(text)).toBeNull()
    }
    expect(screen.getAllByRole('button', { name: 'More information' }).length).toBeGreaterThanOrEqual(7)
  })

  it('the no-window switch is on for a headless config, and off writes Kiro’s Chromium', async () => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { choice: 'headless' } }))
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    const sw = screen.getByRole('switch', { name: 'Run with no window' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(sw)
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.choice', 'kiro_chromium'))
  })

  it('Redownload asks the gateway to REPLACE the downloaded Chromium, not to reuse it', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    fireEvent.click(screen.getByTestId('browser-redownload'))
    await waitFor(() => expect(mocked.installBrowserHarness).toHaveBeenCalledWith({ components: ['chromium'], refresh: true }))
  })

  it('a failed job on an installed engine is an ErrorNotice on the Engine row, not a red card', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({
      install_job: { status: 'failed', stage: 'downloading', error_detail: 'downloading: 403 from storage.googleapis.com' },
    }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-state')).toBe('connected')
    await openMore()
    const notice = await screen.findByTestId('browser-engine-install-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('403 from storage.googleapis.com')
  })

  it('lists the origins as chips, adds a normalized entry, refuses a bad one, and removes', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    expect(screen.getByText('localhost:5173')).toBeTruthy()
    const input = screen.getByPlaceholderText('localhost:5173') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'http://127.0.0.1:3000/' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('dashboard.browser_local_origins', ['localhost:5173', '127.0.0.1:3000']))
    expect(input.value).toBe('')

    fireEvent.change(input, { target: { value: 'example.com:80' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    expect(screen.getByTestId('browser-origin-invalid')).toBeTruthy()
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect(mocked.patchConfig).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Remove localhost:5173' }))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('dashboard.browser_local_origins', []))
  })

  it('a duplicate add writes nothing', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    fireEvent.change(screen.getByPlaceholderText('localhost:5173'), { target: { value: 'LOCALHOST:5173' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    expect(mocked.patchConfig).not.toHaveBeenCalled()
  })
})

describe('BrowserPanel - failures', () => {
  it('a failed first status read says so with a hand-off', async () => {
    mocked.getBrowserStatus.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<BrowserPanel />)
    await screen.findByText('gateway down')
    expect(screen.queryByTestId('browser-primary-card')).toBeNull()
  })

  it('a config read failure disables the config controls but keeps the status', async () => {
    mocked.kirocrewConfig.mockRejectedValue(new Error('config unreadable'))
    renderWithProviders(<BrowserPanel />)
    await screen.findByText('config unreadable')
    expect(screen.getByTestId('browser-status-card')).toBeTruthy()
    expect(screen.getByRole('switch', { name: 'Show the live view in chat' }).getAttribute('aria-disabled')).toBe('true')
  })

  it('re-renders its copy on a language switch without remounting', async () => {
    const { rerender } = renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(<BrowserPanel />)
    expect(screen.getAllByText('你的 Chrome').length).toBeGreaterThan(0)
    expect(screen.getByTestId('browser-status-card')).toBe(card)
  })
})

describe('BrowserPanel - sign-in for Kiro’s Chromium', () => {
  const cred = (over: Record<string, unknown> = {}) => ({
    provider: 'Acme SSO', enabled: true, domains: ['example.com'], state: 'applied', reason: null, applied: 3, ...over,
  })

  it('with no provider there is no sign-in switch and no domains editor', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred({ provider: null, state: 'none' }) }))
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    expect(document.querySelector('[data-setting-key="browser.credentials.enabled"]')).toBeNull()
    expect(screen.queryByRole('switch', { name: /sign-in/ })).toBeNull()
    await openMore()
    expect(screen.queryByText('Sign-in sites')).toBeNull()
  })

  it('with a provider: the switch follows the effective setting, and a flip writes it and re-reads the status', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred() }))
    renderWithProviders(<BrowserPanel />)
    const sw = await screen.findByRole('switch', { name: 'Use Acme SSO sign-in' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('3 cookies applied')).toBeTruthy()
    fireEvent.click(sw)
    const reads = mocked.getBrowserStatus.mock.calls.length
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.credentials.enabled', false))
    await waitFor(() => expect(mocked.getBrowserStatus.mock.calls.length).toBeGreaterThan(reads))
  })

  it('one cookie reads singular; a provider asking to renew is quoted verbatim, else the catalog line', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred({ applied: 1 }) }))
    const first = renderWithProviders(<BrowserPanel />)
    expect(await screen.findByText('1 cookie applied')).toBeTruthy()
    first.unmount()
    mocked.getBrowserStatus.mockResolvedValue(status({
      credentials: cred({ state: 'needs_refresh', provider_status: { needs_refresh: true, message: 'Run acme-login again' } }),
    }))
    const second = renderWithProviders(<BrowserPanel />)
    expect(await screen.findByText('Run acme-login again')).toBeTruthy()
    second.unmount()
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred({ state: 'needs_refresh', provider_status: null }) }))
    renderWithProviders(<BrowserPanel />)
    expect(await screen.findByText('The sign-in from Acme SSO has run out. Renew it there.')).toBeTruthy()
  })

  it('an unset switch reads the edition default the status reports', async () => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { choice: 'auto', credentials: { enabled: null, domains: [] } } }))
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred({ enabled: true, state: 'pending' }) }))
    renderWithProviders(<BrowserPanel />)
    const sw = await screen.findByRole('switch', { name: 'Use Acme SSO sign-in' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('Used from the next page Kiro’s Chromium opens')).toBeTruthy()
  })

  it('a failed apply is an ErrorNotice with the reason code', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred({ state: 'error', reason: 'provider_failed' }) }))
    renderWithProviders(<BrowserPanel />)
    const notice = await screen.findByTestId('browser-credentials-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('provider_failed')
  })

  it('domains under More: a domain is stored lowercase, an IP or a port is refused before any write', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ credentials: cred() }))
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    await openMore()
    expect(screen.getByText('Following Acme SSO’s defaults')).toBeTruthy()
    const input = screen.getByPlaceholderText('example.com') as HTMLInputElement
    for (const bad of ['10.0.0.1', 'example.com:443', '*.example.com', 'intranet']) {
      fireEvent.change(input, { target: { value: bad } })
      fireEvent.click(screen.getByTestId('browser-cred-domain-add'))
      expect(screen.getByTestId('browser-cred-domain-invalid')).toBeTruthy()
    }
    expect(mocked.patchConfig).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: ' .Example.COM ' } })
    fireEvent.click(screen.getByTestId('browser-cred-domain-add'))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.credentials.domains', ['example.com']))
  })
})

describe('BrowserPanel under a policy that turns browsing off', () => {
  it('says so and offers no install, connect or setting row', async () => {
    mocked.dashboardConfig.mockResolvedValue({ browse_enabled: false })
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false }))
    renderWithProviders(<BrowserPanel />)
    expect(await screen.findByTestId('browser-policy-off')).toHaveTextContent(
      "Browsing is turned off by your organization's policy",
    )
    expect(screen.queryByTestId('browser-primary-card')).toBeNull()
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    expect(mocked.getBrowserStatus).not.toHaveBeenCalled()
  })

  it('keeps the page as it was when the policy permits', async () => {
    renderWithProviders(<BrowserPanel />)
    expect(await screen.findByTestId('browser-primary-card')).toBeInTheDocument()
    expect(screen.queryByTestId('browser-policy-off')).toBeNull()
  })
})
