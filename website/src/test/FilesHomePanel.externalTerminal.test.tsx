/**
 * The Files tab's "Open in external terminal" row.
 *
 * The row is an opt-in beside the integrated terminal, never in its place:
 * with no app picked the Project actions menu is exactly what it was, and the
 * row appears only in the desktop shell, against a gateway on this machine,
 * once an app is picked. A refused or failed launch falls back to the
 * integrated terminal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const H = vi.hoisted(() => ({
  config: {} as Record<string, unknown>,
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    revealPath: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api, ApiError: class ApiError extends Error {} }))

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: () => <div data-testid="tree" />,
}))

import FilesHomePanel from '../pages/chat/FilesHomePanel'

const DIR = '/repo/my-project'
const GRANT = 'g'.repeat(43)
const TERMINAL_ROW = 'Open terminal in the project directory'
const EXTERNAL_ROW = 'Open in kitty (separate window)'

/** Bound for every wait in this suite, so none relies on the 1,000 ms
 *  default. The external-terminal checks are a chained query: the desktop
 *  shell's detection (`externalTerminalAPI.detect`) must answer "capable"
 *  before the config query (`api.kirocrewConfig`) is enabled, and only that
 *  answer's picked app renders the row; either read failing renders the
 *  check-failed notice instead. A click on the row runs a second chain: the
 *  grant request, then the shell's launch, then the fallback. The menu and the
 *  file tree wait on the panel's own tree query. The default is a bet on
 *  runner speed; this ceiling turns a lost run into a named failure. */
const EXTERNAL_ROW_WAIT = { timeout: 5000 }

const header = () => screen.getByText('Files').parentElement as HTMLElement

type Bridge = { detect: ReturnType<typeof vi.fn>; open: ReturnType<typeof vi.fn> }
let bridge: Bridge | undefined
let fetchMock: ReturnType<typeof vi.fn>

function installBridge(openResult: unknown = { ok: true, app: 'kitty' }) {
  bridge = {
    detect: vi.fn().mockResolvedValue({ apps: [{ id: 'kitty', label: 'kitty' }], capable: true }),
    open: vi.fn().mockResolvedValue(openResult),
  }
  ;(window as { externalTerminalAPI?: Bridge }).externalTerminalAPI = bridge
}

function pick(app: string | undefined) {
  H.config = app === undefined ? {} : { dashboard: { terminal: { external_app: app } } }
}

/** `null` mounts the panel the way a host with no integrated terminal does. */
function mount(onOpenTerminal: (() => void) | null = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onExternalTerminalFailed = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <FilesHomePanel
        projectDir={DIR}
        onFileOpen={vi.fn()}
        onOpenTerminal={onOpenTerminal ?? undefined}
        onExternalTerminalFailed={onExternalTerminalFailed}
      />
    </QueryClientProvider>,
  )
  return { onOpenTerminal, onExternalTerminalFailed }
}

function openProjectActions() {
  fireEvent.keyDown(within(header()).getByLabelText('Project actions'), { key: 'Enter' })
}

/** The menu's rows by their first line, in order, once it has opened. */
async function menuRows() {
  await screen.findByText(TERMINAL_ROW, undefined, EXTERNAL_ROW_WAIT)
  return screen.getAllByRole('menuitem').map(el => el.textContent ?? '')
}

beforeEach(() => {
  H.api.projectTree.mockReset().mockResolvedValue({ root: DIR, paths: [], repo: true })
  H.api.projectGitStatus.mockReset().mockResolvedValue({ repo: true, files: [] })
  H.api.revealPath.mockReset().mockResolvedValue(undefined)
  H.api.kirocrewConfig.mockReset().mockImplementation(async () => H.config)
  brandingEnv.directLocal = true
  pick(undefined)
  bridge = undefined
  delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ grant: GRANT, app: 'kitty' }) })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
})

describe('with no external app picked (the default)', () => {
  it('leaves the Project actions menu and its terminal row exactly as they were', async () => {
    installBridge()
    pick(undefined)
    const { onOpenTerminal } = mount()
    await waitFor(() => expect(H.api.kirocrewConfig).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    openProjectActions()
    const rows = await menuRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]).toContain(TERMINAL_ROW)
    expect(screen.queryByText(EXTERNAL_ROW)).toBeNull()
    fireEvent.click(screen.getByText(TERMINAL_ROW))
    expect(onOpenTerminal).toHaveBeenCalledTimes(1)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(bridge!.open).not.toHaveBeenCalled()
  })

  it('treats an empty pick as unset', async () => {
    installBridge()
    pick('')
    mount()
    await waitFor(() => expect(H.api.kirocrewConfig).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
  })
})

describe('with an app picked', () => {
  it('adds the row below the integrated terminal row on the desktop app', async () => {
    installBridge()
    pick('kitty')
    mount()
    openProjectActions()
    await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT)
    const rows = screen.getAllByRole('menuitem').map(el => el.textContent ?? '')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain(TERMINAL_ROW)
    expect(rows[1]).toContain(EXTERNAL_ROW)
  })

  it('names the picked app by the label the desktop shell detected', async () => {
    installBridge()
    bridge!.detect.mockResolvedValue({ apps: [{ id: 'wezterm', label: 'WezTerm' }], capable: true })
    pick('wezterm')
    mount()
    openProjectActions()
    expect(await screen.findByText('Open in WezTerm (separate window)', undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
    expect(screen.getByText('In a separate window: opens WezTerm, the terminal app you picked in Settings, in this project’s folder.')).toBeInTheDocument()
  })

  it('shows no row for a pick that is no longer installed', async () => {
    installBridge()
    bridge!.detect.mockResolvedValue({ apps: [{ id: 'wezterm', label: 'WezTerm' }], capable: true })
    pick('kitty')
    mount()
    await waitFor(() => expect(bridge!.detect).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
    expect(screen.queryByText(/^Open in /)).toBeNull()
  })

  it('is absent in the browser build, where there is no launch bridge', async () => {
    pick('kitty')
    mount()
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
    expect(screen.queryByText(EXTERNAL_ROW)).toBeNull()
    expect(H.api.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('is absent for a gateway that is not on this machine', async () => {
    installBridge()
    pick('kitty')
    brandingEnv.directLocal = false
    mount()
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
    expect(screen.queryByText(EXTERNAL_ROW)).toBeNull()
  })

  // A desktop window reaching a remote crew through an SSH tunnel reads as a
  // direct-local request to that crew (sshd connects from loopback with no
  // forwarding headers), so the panel does ask the shell. The shell answers
  // "not capable": no row, and no notice whose Retry could never clear.
  it('is absent, with no notice, for a window the desktop shell answers as not capable', async () => {
    installBridge()
    bridge!.detect.mockResolvedValue({ apps: [], capable: false })
    pick('kitty')
    mount()
    await waitFor(() => expect(bridge!.detect).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
    expect(screen.queryByText(EXTERNAL_ROW)).toBeNull()
    expect(screen.queryByTestId('files-home-external-terminal-check-error')).toBeNull()
    expect(H.api.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('reports no failed setting read for a window the desktop shell answers as not capable', async () => {
    installBridge()
    bridge!.detect.mockResolvedValue({ apps: [], capable: false })
    H.api.kirocrewConfig.mockRejectedValue(new Error('HTTP 500'))
    mount()
    await waitFor(() => expect(bridge!.detect).toHaveBeenCalled(), EXTERNAL_ROW_WAIT)
    await waitFor(() => expect(screen.getByTestId('tree')).toBeInTheDocument(), EXTERNAL_ROW_WAIT)
    expect(screen.queryByTestId('files-home-external-terminal-check-error')).toBeNull()
  })

  it('is absent when the host serves no integrated terminal', async () => {
    installBridge()
    pick('kitty')
    mount(null)
    await waitFor(() => expect(screen.getByTestId('tree')).toBeInTheDocument(), EXTERNAL_ROW_WAIT)
    expect(within(header()).queryByLabelText('Project actions')).toBeNull()
  })

  it('asks the gateway for a grant and hands only the grant to the desktop shell', async () => {
    installBridge()
    pick('kitty')
    const { onOpenTerminal } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(bridge!.open).toHaveBeenCalledWith(GRANT), EXTERNAL_ROW_WAIT)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/terminal/external')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ cwd: DIR })
    expect(onOpenTerminal).not.toHaveBeenCalled()
  })

  it('falls back to the integrated terminal when the gateway refuses', async () => {
    installBridge()
    pick('kitty')
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ code: 'external_terminal_not_local' }),
    })
    const { onOpenTerminal, onExternalTerminalFailed } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(onOpenTerminal).toHaveBeenCalledTimes(1), EXTERNAL_ROW_WAIT)
    expect(bridge!.open).not.toHaveBeenCalled()
    expect(onExternalTerminalFailed).toHaveBeenCalledWith({
      message: "This window can't open apps on this computer, so the built-in terminal opened in a new tab instead.",
      offerSettings: false,
    })
  })

  it('falls back to the integrated terminal when the launch fails', async () => {
    installBridge({ ok: false, error: 'launch-failed' })
    pick('kitty')
    const { onOpenTerminal, onExternalTerminalFailed } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(onOpenTerminal).toHaveBeenCalledTimes(1), EXTERNAL_ROW_WAIT)
    expect(bridge!.open).toHaveBeenCalledWith(GRANT)
    expect(onExternalTerminalFailed).toHaveBeenCalledWith({
      message: "kitty didn't start, so the built-in terminal opened in a new tab instead.",
      offerSettings: false,
    })
  })

  it('says the app was turned off after the menu opened when the gateway finds no app picked', async () => {
    installBridge()
    pick('kitty')
    fetchMock.mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ code: 'external_terminal_not_configured' }),
    })
    const { onOpenTerminal, onExternalTerminalFailed } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(onOpenTerminal).toHaveBeenCalledTimes(1), EXTERNAL_ROW_WAIT)
    expect(onExternalTerminalFailed).toHaveBeenCalledWith({
      message: 'kitty was turned off in Settings after this menu opened, so the built-in terminal opened in a new tab instead. To use it, pick it again in Settings → Display → Terminal.',
      offerSettings: true,
    })
  })

  it('says the app is no longer installed when the desktop shell cannot find it', async () => {
    installBridge({ ok: false, error: 'no-app' })
    pick('kitty')
    const { onOpenTerminal, onExternalTerminalFailed } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(onOpenTerminal).toHaveBeenCalledTimes(1), EXTERNAL_ROW_WAIT)
    expect(bridge!.open).toHaveBeenCalledWith(GRANT)
    expect(onExternalTerminalFailed).toHaveBeenCalledWith({
      message: 'kitty is no longer installed on this computer, so the built-in terminal opened in a new tab instead. Install it again, or pick another app in Settings → Display → Terminal.',
      offerSettings: true,
    })
  })

  it('falls back when the desktop shell rejects the request', async () => {
    installBridge()
    bridge!.open.mockRejectedValue(new Error('external-terminal:open is restricted to the local dashboard'))
    pick('kitty')
    const { onOpenTerminal } = mount()
    openProjectActions()
    fireEvent.click(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT))
    await waitFor(() => expect(onOpenTerminal).toHaveBeenCalledTimes(1), EXTERNAL_ROW_WAIT)
  })

  it('reports a failed detection instead of silently dropping the row', async () => {
    installBridge()
    bridge!.detect.mockRejectedValue(new Error('probe failed'))
    pick('kitty')
    mount()
    expect(await screen.findByTestId('files-home-external-terminal-check-error', undefined, EXTERNAL_ROW_WAIT)).toHaveTextContent(
      "Couldn't load your terminal app setting, so the option to open it is hidden for now.",
    )
    openProjectActions()
    expect(await menuRows()).toHaveLength(1)
  })

  it('retries both checks from the notice and shows the row once they answer', async () => {
    installBridge()
    bridge!.detect.mockRejectedValueOnce(new Error('busy'))
    pick('kitty')
    mount()
    const notice = await screen.findByTestId('files-home-external-terminal-check-error', undefined, EXTERNAL_ROW_WAIT)
    const configReads = H.api.kirocrewConfig.mock.calls.length
    fireEvent.click(within(notice.parentElement as HTMLElement).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByTestId('files-home-external-terminal-check-error')).toBeNull(), EXTERNAL_ROW_WAIT)
    expect(bridge!.detect).toHaveBeenCalledTimes(2)
    expect(H.api.kirocrewConfig.mock.calls.length).toBeGreaterThan(configReads)
    openProjectActions()
    expect(await screen.findByText(EXTERNAL_ROW, undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
  })

  // `max-two-buttons-per-row` (website/AUTOSDE.yaml, blocking): the notice's
  // own row carries the hand-off and the dismiss, so Retry takes a line of its
  // own under it rather than standing beside them as a third peer.
  it('keeps the check-failed row to two controls, with Retry on its own line', async () => {
    installBridge()
    bridge!.detect.mockRejectedValue(new Error('busy'))
    pick('kitty')
    mount()
    const notice = await screen.findByTestId('files-home-external-terminal-check-error', undefined, EXTERNAL_ROW_WAIT)
    expect(within(notice).getAllByRole('button')).toHaveLength(2)
    const group = notice.parentElement as HTMLElement
    const retry = within(group).getByRole('button', { name: 'Retry' })
    expect(notice.contains(retry)).toBe(false)
    expect(group.className.split(/\s+/)).toContain('flex-col')
  })

  // The Retry is the shared Btn primitive, not a hand-rolled button: this
  // asserts the pressed-state class only Btn contributes.
  it('renders the Retry through the shared Btn primitive', async () => {
    installBridge()
    bridge!.detect.mockRejectedValue(new Error('busy'))
    pick('kitty')
    mount()
    const notice = await screen.findByTestId('files-home-external-terminal-check-error', undefined, EXTERNAL_ROW_WAIT)
    const retry = within(notice.parentElement as HTMLElement).getByRole('button', { name: 'Retry' })
    expect(retry.className).toContain('active:scale-[0.97]')
  })

  it('reports a failed setting read the same way', async () => {
    installBridge()
    H.api.kirocrewConfig.mockRejectedValue(new Error('HTTP 500'))
    mount()
    expect(await screen.findByTestId('files-home-external-terminal-check-error', undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
  })

  it('reports nothing in the browser build, where nothing is checked', async () => {
    H.api.kirocrewConfig.mockRejectedValue(new Error('HTTP 500'))
    mount()
    await waitFor(() => expect(screen.getByTestId('tree')).toBeInTheDocument(), EXTERNAL_ROW_WAIT)
    expect(screen.queryByTestId('files-home-external-terminal-check-error')).toBeNull()
  })
})
