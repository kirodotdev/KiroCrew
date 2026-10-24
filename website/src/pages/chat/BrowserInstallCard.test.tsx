import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      getBrowserInstall: vi.fn(),
      installBrowserCli: vi.fn(),
    },
  }
})

import { api, type BrowserInstallData, type BrowserInstallJob } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { hasBrowserInstallMarker } from '../../utils/detectBrowserInstallMarker'
import { INSTALL_KEY } from '../settings/browserInstallState'
import BrowserInstallCard from './BrowserInstallCard'
import { browserInstallCardState } from './browserInstallCardState'

const MARKER = '<!-- kirocrew:browser-install -->'
const REPLY = `Browser support needs a one-time install.\n\n${MARKER}\n`

function data(overrides: Partial<BrowserInstallData> = {}): BrowserInstallData {
  return {
    installed: false,
    cli_path: null,
    cli_version: null,
    node_ok: true,
    node_version: '22.0.0',
    browser_ok: false,
    installing: false,
    last_error: null,
    token: false,
    install_job: null,
    ...overrides,
  }
}

function job(overrides: Partial<BrowserInstallJob> = {}): BrowserInstallJob {
  return {
    id: 'j1',
    kind: 'cli_setup',
    engine: null,
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

const card = () => screen.getByTestId('browser-install-card')

describe('hasBrowserInstallMarker', () => {
  it('finds the hidden marker', () => {
    expect(hasBrowserInstallMarker(REPLY)).toBe(true)
    expect(hasBrowserInstallMarker('<!--kirocrew:browser-install-->')).toBe(true)
  })

  it('ignores a marker the reader sees as code, and other markers', () => {
    expect(hasBrowserInstallMarker('```\n' + MARKER + '\n```')).toBe(false)
    expect(hasBrowserInstallMarker('Write `' + MARKER + '` to offer it.')).toBe(false)
    expect(hasBrowserInstallMarker('<!-- kirocrew:open-file path="/a/b.md" -->')).toBe(false)
    expect(hasBrowserInstallMarker('')).toBe(false)
  })
})

describe('browserInstallCardState', () => {
  it('offers the install when nothing is installed or running', () => {
    expect(browserInstallCardState(data(), null, 0)).toEqual({ kind: 'needed' })
  })

  it('reports a running job with its stage', () => {
    expect(browserInstallCardState(data({ installing: true, install_job: job() }), null, 0))
      .toMatchObject({ kind: 'running', stage: 'downloading_browser' })
  })

  it('treats this tab’s unanswered click as running', () => {
    expect(browserInstallCardState(data(), { kind: 'cli_setup' }, 1)).toMatchObject({ kind: 'running', stage: 'preparing' })
  })

  it('is installed only once the CLI and its browser are both present', () => {
    expect(browserInstallCardState(data({ installed: true, browser_ok: true }), null, 0)).toEqual({ kind: 'installed' })
    expect(browserInstallCardState(data({ installed: true, browser_ok: false }), null, 0)).toEqual({ kind: 'needed' })
  })

  it('surfaces a failed CLI setup with its detail', () => {
    const failed = job({ status: 'failed', error_detail: 'npm not found; install Node.js 20 or newer first' })
    expect(browserInstallCardState(data({ install_job: failed }), null, 0))
      .toEqual({ kind: 'failed', detail: 'npm not found; install Node.js 20 or newer first' })
  })
})

describe('BrowserInstallCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders nothing, and reads no status, without the marker', () => {
    renderWithProviders(<BrowserInstallCard content="Here is the page summary." />)
    expect(screen.queryByTestId('browser-install-card')).toBeNull()
    expect(api.getBrowserInstall).not.toHaveBeenCalled()
  })

  it('installs on click and shows progress, without installing on render', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    vi.mocked(api.installBrowserCli).mockResolvedValue(data({ installing: true, install_job: job() }))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    const install = await screen.findByTestId('browser-install-card-install')
    expect(api.installBrowserCli).not.toHaveBeenCalled()
    expect(card().dataset.state).toBe('needed')
    // From here the gateway reports the job it started.
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installing: true, install_job: job() }))
    fireEvent.click(install)
    await waitFor(() => expect(api.installBrowserCli).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(card().dataset.state).toBe('running'))
  })

  it('offers to continue once installed, pre-filling the composer rather than sending', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installed: true, browser_ok: true }))
    const prefill = vi.fn()
    window.addEventListener('mc:prefill-composer', prefill)
    try {
      renderWithProviders(<BrowserInstallCard content={REPLY} />)
      const cont = await screen.findByTestId('browser-install-card-continue')
      // The label says what the click does: it writes a message, it does not send one.
      expect(cont.textContent).toBe('Draft a message to continue')
      fireEvent.click(cont)
      expect(prefill).toHaveBeenCalledTimes(1)
      const text = (prefill.mock.calls[0][0] as CustomEvent).detail.text
      expect(typeof text).toBe('string')
      expect(text.length).toBeGreaterThan(0)
    } finally {
      window.removeEventListener('mc:prefill-composer', prefill)
    }
  })

  it('replaces the Continue button with the confirmation once clicked', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installed: true, browser_ok: true }))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    fireEvent.click(await screen.findByTestId('browser-install-card-continue'))
    expect(await screen.findByText('Added to your message box. Send it to continue.')).toBeTruthy()
    expect(screen.queryByTestId('browser-install-card-continue')).toBeNull()
    expect(card().querySelector('button')).toBeNull()
  })

  const OWNER_ONLY =
    'Only the person who set up Kiro Crew on this computer can install this. Ask them to install it from Kiro Crew browser settings.'

  it('replaces Install with who can install it when the owner gate refuses, keeping the Settings link', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    vi.mocked(api.installBrowserCli).mockRejectedValue(
      new ApiError(403, 'owner authorization required', '{"code":"owner_only"}'),
    )
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    fireEvent.click(await screen.findByTestId('browser-install-card-install'))
    const notice = await screen.findByTestId('browser-install-card-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain(OWNER_ONLY)
    expect(notice.textContent).not.toContain('owner authorization required')
    // No agent hand-off: leaving could discard the composer draft.
    expect(notice.querySelector('button')).toBeNull()
    // A retry would only repeat the same 403, so nothing offers one.
    expect(screen.queryByTestId('browser-install-card-install')).toBeNull()
    expect(card().querySelector('button')).toBeNull()
    expect(screen.getByTestId('browser-install-card-settings').getAttribute('href')).toBe('/settings/browser')
  })

  it('drops the Retry button too when the owner gate refuses a retry after a failed install', async () => {
    const failed = job({ status: 'failed', error_detail: 'npm not found; install Node.js 20 or newer first' })
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ install_job: failed }))
    vi.mocked(api.installBrowserCli).mockRejectedValue(new ApiError(403, 'owner authorization required', '{}'))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    fireEvent.click(await screen.findByTestId('browser-install-card-install'))
    expect((await screen.findByTestId('browser-install-card-error')).textContent).toContain(OWNER_ONLY)
    expect(screen.queryByTestId('browser-install-card-install')).toBeNull()
    expect(screen.getByTestId('browser-install-card-settings').getAttribute('href')).toBe('/settings/browser')
  })

  it.each([
    ['a server error', new ApiError(500, 'install crashed', '{}'), 'install crashed'],
    ['a lost answer', new Error('network request failed'), 'network request failed'],
    ['a lapsed dashboard sign-in', new ApiError(403, 'sign in again', '{}', true), 'sign in again'],
  ])('keeps a working Install button beside the notice after %s', async (_label, err, text) => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    vi.mocked(api.installBrowserCli).mockRejectedValue(err)
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    fireEvent.click(await screen.findByTestId('browser-install-card-install'))
    const notice = await screen.findByTestId('browser-install-card-error')
    expect(notice.textContent).toContain(text)
    expect(notice.textContent).not.toContain(OWNER_ONLY)
    const install = screen.getByTestId('browser-install-card-install') as HTMLButtonElement
    expect(install.disabled).toBe(false)
    fireEvent.click(install)
    await waitFor(() => expect(api.installBrowserCli).toHaveBeenCalledTimes(2))
  })

  it('shows a rejected status read through ErrorNotice without a hand-off', async () => {
    vi.mocked(api.getBrowserInstall).mockRejectedValue(new Error('gateway status read failed'))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    const notice = await screen.findByTestId('browser-install-card-status-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain("Couldn't check browser support on this computer.")
    expect(notice.textContent).toContain('gateway status read failed')
    expect(notice.querySelector('button')).toBeNull()
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(screen.getByTestId('browser-install-card-settings').getAttribute('href')).toBe('/settings/browser')
  })

  it('shows a failed refetch over cached running status and recovers on the next read', async () => {
    const running = data({ installing: true, install_job: job() })
    vi.mocked(api.getBrowserInstall).mockResolvedValue(running)
    const { queryClient } = renderWithProviders(<BrowserInstallCard content={REPLY} />)
    await waitFor(() => expect(card().dataset.state).toBe('running'))

    vi.mocked(api.getBrowserInstall).mockRejectedValue(new Error('connection lost during install'))
    await queryClient.refetchQueries({ queryKey: INSTALL_KEY })
    const notice = await screen.findByTestId('browser-install-card-status-error')
    expect(card().dataset.state).toBe('unavailable')
    expect(notice.textContent).toContain('connection lost during install')
    expect(queryClient.getQueryData(INSTALL_KEY)).toEqual(running)

    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installed: true, browser_ok: true }))
    await queryClient.refetchQueries({ queryKey: INSTALL_KEY })
    await waitFor(() => expect(card().dataset.state).toBe('installed'))
    expect(screen.queryByTestId('browser-install-card-status-error')).toBeNull()
    expect(screen.getByTestId('browser-install-card-continue')).toBeTruthy()
  })

  it('leads a failed install with the next step, then the raw detail, Settings first', async () => {
    const failed = job({ status: 'failed', error_detail: 'npm not found; install Node.js 20 or newer first' })
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ install_job: failed }))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    const detail = await screen.findByTestId('browser-install-card-failure-detail')
    expect(card().dataset.state).toBe('failed')
    expect(detail.getAttribute('role')).toBe('alert')
    expect(detail.textContent).toContain('npm not found')
    const next = screen.getByText(
      "Kiro Crew browser settings lists what's missing, such as Node.js, and how to install it. Then try again.",
    )
    // The plain-language step comes before the installer's raw output.
    expect(next.compareDocumentPosition(detail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const settings = screen.getByTestId('browser-install-card-settings')
    const retry = screen.getByTestId('browser-install-card-install')
    expect(settings.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(settings.className).toContain('border-accent')
    expect(retry.className).not.toContain('border-accent')
  })

  it('opens Settings -> Browser as an in-app route change, not a page load', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    function RouteProbe() {
      const loc = useLocation()
      return <div data-testid="route-probe" data-path={loc.pathname} />
    }
    renderWithProviders(<><BrowserInstallCard content={REPLY} /><RouteProbe /></>, { route: '/chat' })
    const settings = await screen.findByTestId('browser-install-card-settings')
    expect(settings.getAttribute('href')).toBe('/settings/browser')
    expect(screen.getByTestId('route-probe').dataset.path).toBe('/chat')
    // fireEvent.click returns false when the handler cancelled the browser's own
    // navigation, which is what keeps the page (and its composer draft) loaded.
    expect(fireEvent.click(settings)).toBe(false)
    await waitFor(() => expect(screen.getByTestId('route-probe').dataset.path).toBe('/settings/browser'))
  })

  it('falls back to a plain link outside a Router', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <BrowserInstallCard content={REPLY} />
      </QueryClientProvider>,
    )
    const settings = await screen.findByTestId('browser-install-card-settings')
    expect(settings.tagName).toBe('A')
    expect(settings.getAttribute('href')).toBe('/settings/browser')
  })

  it('re-reads the status on mount even under a never-stale global default', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    const view = renderWithProviders(<BrowserInstallCard content={REPLY} />, {
      queryDefaults: { staleTime: Infinity },
    })
    await screen.findByTestId('browser-install-card-install')
    expect(api.getBrowserInstall).toHaveBeenCalledTimes(1)
    view.rerender(<div />) // unmount the card; the cached answer stays
    view.rerender(<BrowserInstallCard content={REPLY} />)
    await waitFor(() => expect(api.getBrowserInstall).toHaveBeenCalledTimes(2))
  })
})
