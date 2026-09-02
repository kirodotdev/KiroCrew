import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { McpServer } from '../types'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))

// The modal has its own suite (McpBrowserModal.test.tsx) — probe only the
// open/close wiring here.
vi.mock('../components/McpBrowserModal', () => ({
  default: ({ open }: { open: boolean }) => (
    <div data-testid="mcp-browser-modal" data-open={String(open)} />
  ),
}))

import McpTab from '../pages/overview/McpTab'
import { MemoryRouter } from 'react-router-dom'

const server = (name: string): McpServer => ({
  name, command: `${name}-cmd`, status: 'ok', source: 'kirocrew', enabled: true, tools: ['t1'],
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // MemoryRouter because the sign-in guidance renders a <Link> to the chat route:
  // react-router's Link reads its context unconditionally and throws without a
  // router, so this wrapper is load-bearing rather than boilerplate.
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([server('alpha'), server('beta')])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  // connections_ui is launch-held OFF by default; the in-place sign-in only
  // renders when a test opts it on. Off ⇒ managed rows fall to the chat prose.
  mockApi.kirocrewConfig.mockResolvedValue({})
})

describe('McpTab restructure', () => {
  it('header shows MCP Servers with the installed count', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument())
  })

  it('the inline registry card is gone', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument())
    expect(screen.queryByText('Browse Integrations')).not.toBeInTheDocument()
    expect(screen.queryByText('Installed Integrations')).not.toBeInTheDocument()
  })

  it('Add Server button opens the browser modal', async () => {
    renderTab()
    const addBtn = await screen.findByRole('button', { name: /Add Server/ })
    expect(screen.getByTestId('mcp-browser-modal')).toHaveAttribute('data-open', 'false')
    fireEvent.click(addBtn)
    expect(screen.getByTestId('mcp-browser-modal')).toHaveAttribute('data-open', 'true')
  })

  it('keeps the installed-servers table as the page body', async () => {
    renderTab()
    // Both configured servers render as table rows (name in a <code> cell —
    // the status badge chips also contain the name, so scope the query).
    await waitFor(() => expect(screen.getByText('alpha', { selector: 'code' })).toBeInTheDocument())
    expect(screen.getByText('beta', { selector: 'code' })).toBeInTheDocument()
    expect(screen.getByText('alpha-cmd')).toBeInTheDocument()
    // Uninstall stays in the table (per-row action), not in the modal.
    expect(screen.getAllByRole('button', { name: 'Uninstall' })).toHaveLength(2)
  })

  it('badges a registry-managed remote server', async () => {
    mockApi.mcpServers.mockResolvedValue([{
      ...server('notion'),
      command: '',
      url: 'https://mcp.notion.com/mcp',
    }])
    renderTab()
    await waitFor(() => expect(screen.getByText('Managed by Connections')).toBeInTheDocument())
  })
})

/**
 * #1853: the status probe runs without the OAuth token kiro-cli holds, so a
 * remote OAuth server answers it with 401 while the agent runtime calls the same
 * server fine. The gateway reports that as `needs_auth`, and the table must say
 * only what it knows — the authorization is not visible from here — rather than
 * calling a working server broken or claiming it needs a grant it may already have.
 */
describe('McpTab needs_auth status', () => {
  const remote = (status: string): McpServer => ({
    name: 'atlassian',
    command: '',
    url: 'https://mcp.atlassian.com/v1/sse',
    status,
    source: 'mcp.json',
    enabled: true,
    tools: [],
  })

  it('renders the not-verified state, not an error badge', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    await waitFor(() => expect(screen.getByText('Not verified')).toBeInTheDocument())
    // The badge carries the warn tone, never the error tone.
    expect(screen.getByText('Not verified').className).toContain('text-warn')
    expect(screen.getByText('Not verified').className).not.toContain('text-danger')
    // Neither the old "Error" label nor the uninformative "Unknown" fallback.
    expect(screen.queryByText('Error')).not.toBeInTheDocument()
    expect(screen.queryByText('Unknown')).not.toBeInTheDocument()
  })

  it('explains the unverifiable status on hover, naming the server', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    const badge = await screen.findByText('Not verified')
    const hint = badge.getAttribute('title') || ''
    // Says who holds the token and that a working server is still working —
    // the two facts that make the badge honest instead of alarming.
    expect(hint).toContain('atlassian')
    expect(hint).toContain('Kiro CLI')
    expect(hint).toMatch(/cannot see the authorization/)
  })

  /**
   * With a challenge AND an absent runtime grant, "nobody has signed in" is a
   * fact rather than a guess, so the row names the action. Everything below
   * turns on that pair being present — absent evidence must keep the vaguer
   * wording, because an older gateway sends none and its servers may be fine.
   */
  it('says sign-in is required when the server asked for OAuth and no grant exists', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: false },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
    // Still a warning, never an error: nothing is broken, it just needs a sign-in.
    expect(screen.getByText('Sign-in required').className).toContain('text-warn')
    expect(screen.getByText('Sign-in required').className).not.toContain('text-danger')
  })

  /**
   * A held grant is its own state, not a fallback to the no-evidence one. The
   * probe ships `authGrantPresent` precisely to tell those apart, so collapsing
   * them would leave every completed sign-in ending on the same badge the user
   * started from — a flow with no visible reward.
   *
   * "Signed in" reports the grant kiro-cli holds, which is what was observed. It
   * deliberately does NOT claim the server answers: the probe has no token, so
   * validity is the one thing it cannot check, and the hover says so rather than
   * the badge over-claiming.
   */
  it('reports a held runtime grant as signed in, without claiming it still works', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: true },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Signed in')).toBeInTheDocument())
    expect(screen.queryByText('Sign-in required')).not.toBeInTheDocument()
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
    // Muted, and deliberately neither of the other two tones. Amber is this panel's
    // "you need to act" colour, so a resolved row wearing it is indistinguishable by
    // colour from the one still asking to be signed in; green would claim the server
    // answers, which the probe cannot check without the runtime's token.
    expect(screen.getByText('Signed in').className).toContain('text-[var(--muted)]')
    expect(screen.getByText('Signed in').className).not.toContain('text-warn')
    expect(screen.getByText('Signed in').className).not.toContain('text-ok')
    expect(screen.getByText('Signed in').title).toMatch(/cannot confirm the sign-in is still valid/)
  })

  it('keeps the not-verified wording when the gateway sent no authorization evidence', async () => {
    // An older gateway, or a 401 with no challenge. Telling this user to sign in
    // would be a guess about a server that may already be working.
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    await waitFor(() => expect(screen.getByText('Not verified')).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: /Go to chat/ })).not.toBeInTheDocument()
  })

  /**
   * The sign-in prompt is raised by Kiro CLI while a session brings its MCP
   * servers up, which happens on a turn. Nothing the dashboard can call from
   * this panel starts that, so the row states where the sign-in happens rather
   * than offering a control that cannot perform it.
   */
  it('tells the user where the sign-in happens, and offers no control that cannot do it', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: false },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    // The navigation step is an affordance, not an instruction: it is a link to
    // the chat route because that navigation IS something the panel can perform.
    // Session creation stays in prose because navigating to the route does not
    // itself create one.
    const link = screen.getByRole('link', { name: /Go to chat/ })
    expect(link).toHaveAttribute('href', '/chat')
    // A live session keeps the server set it started with. The chat destination
    // therefore has to tell the user to create a NEW session before sending the
    // turn that raises the OAuth approval prompt.
    expect(screen.getByText(/start a new session, and send any message/)).toBeInTheDocument()
    // The ending is VISIBLE, because a `title` reaches neither a keyboard nor a
    // touch user — and the panel serves from the probe cache for the whole TTL, so
    // someone returning from a completed sign-in would meet a row still reading
    // "Sign-in required" and conclude it had failed. One clause in the cell; the
    // longer form, naming the control and the resulting state, rides the hover.
    expect(screen.getByText(/Then probe to refresh this list/)).toBeInTheDocument()
    expect(screen.getByText('Sign-in required').title).toMatch(
      /use the Probe MCP servers button above this table; this row will then read Signed in/,
    )
    // Still no Authorize control: starting the sign-in is not something this
    // panel can do, and a button here would claim an action it cannot perform.
    expect(screen.queryByRole('button', { name: /Authorize/ })).not.toBeInTheDocument()
  })

  it('does not show the sign-in guidance once a runtime grant exists', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: true },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Signed in')).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: /Go to chat/ })).not.toBeInTheDocument()
  })

  /**
   * #6274: a row that needs a sign-in AND resolves to a curated Connections
   * provider (name === slug AND url === the registry mcp_url) can start the
   * sign-in in place, reusing the headless mint engine — but ONLY when the
   * Connections UI is unlocked (`connections_ui: true`). A non-resolvable row,
   * or the flag held closed, keeps the chat prose unchanged — minting is never
   * offered for arbitrary URLs (parked maintainer decision #4286), and the mint
   * engine is not a released surface while the gallery is held.
   */
  it('offers an in-place Sign in on a resolvable managed row when connections_ui is on', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ connections_ui: true })
    mockApi.mcpServers.mockResolvedValue([
      {
        ...remote('needs_auth'),
        name: 'notion',
        url: 'https://mcp.notion.com/mcp',
        authChallenge: true,
        authGrantPresent: false,
      },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    // The managed row gets the in-place control, not the chat prose.
    await waitFor(() => expect(screen.getByRole('button', { name: /Sign in/ })).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: /Go to chat/ })).not.toBeInTheDocument()
  })

  it('FIX 1: falls back to the chat prose on a resolvable managed row when connections_ui is OFF', async () => {
    // The mint engine is launch-held behind connections_ui. With it off, even a
    // registry-resolvable row must show the same chat guidance a non-registry row
    // does — chat stays the only authorize prompt while the gallery is closed.
    mockApi.kirocrewConfig.mockResolvedValue({}) // flag off (also the default)
    mockApi.mcpServers.mockResolvedValue([
      {
        ...remote('needs_auth'),
        name: 'notion',
        url: 'https://mcp.notion.com/mcp',
        authChallenge: true,
        authGrantPresent: false,
      },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Sign in/ })).not.toBeInTheDocument()
  })

  it('keeps the chat prose on a non-resolvable row even when connections_ui is on', async () => {
    // A server whose URL is NOT the registry mcp_url does not resolve to a
    // provider, so it never gets a mint control — only the chat guidance, flag
    // on or off.
    mockApi.kirocrewConfig.mockResolvedValue({ connections_ui: true })
    mockApi.mcpServers.mockResolvedValue([
      {
        ...remote('needs_auth'),
        name: 'notion',
        url: 'https://self-hosted.example.com/mcp',
        authChallenge: true,
        authGrantPresent: false,
      },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Sign in/ })).not.toBeInTheDocument()
  })

  /**
   * The guidance names the refresh control by the one string that control carries.
   * It renders no visible label, so without a hover title a reader cannot match the
   * instruction to the button it means.
   */
  it('gives the probe control the name its instructions use', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: false },
    ])
    renderTab()

    const probe = await screen.findByRole('button', { name: 'Probe MCP servers' })
    expect(probe).toHaveAttribute('title', 'Probe MCP servers')
  })

  /**
   * `max-two-buttons-per-row` (website/AUTOSDE.yaml) caps a horizontal action
   * group at two siblings. A managed row that needs a sign-in is where a third
   * action would land, so the cap is asserted there.
   */
  it('keeps the action group at two buttons on a managed row that needs sign-in', async () => {
    mockApi.mcpServers.mockResolvedValue([
      {
        ...remote('needs_auth'),
        kirocrewManaged: true,
        authChallenge: true,
        authGrantPresent: false,
      },
    ])
    renderTab()

    const uninstall = await screen.findByRole('button', { name: /Uninstall/ })
    const group = uninstall.closest('div')
    expect(group).not.toBeNull()
    expect(group!.querySelectorAll('button').length).toBeLessThanOrEqual(2)
  })

  it('tells the user a pasted token cannot satisfy an OAuth server', async () => {
    mockApi.mcpServers.mockResolvedValue([
      {
        ...remote('error'),
        error: 'HTTP 401',
        headers: { Authorization: '[REDACTED: credential]' },
        authChallenge: true,
      },
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('HTTP 401')).toBeInTheDocument())
    expect(screen.getByText(/static Authorization header cannot satisfy it/)).toBeInTheDocument()
    // Stored header values are deliberately preserve-only in the editor, so the
    // recovery step must name the supported remove/re-add flow rather than an
    // edit the API will silently restore from disk.
    expect(
      screen.getByText(/Remove and re-add this server without the header, enable it, then start a new chat session/),
    ).toBeInTheDocument()
  })

  it('explains that Online is a host check, and leaves the rest without a hover explanation', async () => {
    // "Online" is the gateway's own probe result and reads as a stronger claim
    // than it is — it says nothing about whether a given chat session mounted
    // the server — so it carries the caveat two words cannot. The other statuses
    // still get none: this stays a named exception rather than blanket hints.
    mockApi.mcpServers.mockResolvedValue([remote('ok')])
    renderTab()

    const badge = await screen.findByText('Online')
    expect(badge).toHaveAttribute('title', expect.stringContaining('gateway started this server'))

    for (const status of ['error', 'outdated', 'disabled'] as const) {
      mockApi.mcpServers.mockResolvedValue([remote(status)])
      const { unmount } = renderTab()
      const other = await screen.findByText(
        status === 'error' ? 'Error' : status === 'outdated' ? 'Outdated' : 'Disabled',
      )
      expect(other).not.toHaveAttribute('title')
      unmount()
    }
  })

  it('still renders a real failure as an error badge with its message', async () => {
    mockApi.mcpServers.mockResolvedValue([{ ...remote('error'), error: 'HTTP 500' }])
    renderTab()

    await waitFor(() => expect(screen.getByText('Error')).toBeInTheDocument())
    expect(screen.getByText('Error').className).toContain('text-danger')
    expect(screen.getByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
  })
})

describe('McpTab declared-vs-handshake status', () => {
  it('a declared server shows "Declared", never the green "Online"', async () => {
    // probeMode 'declared' means the tool list came from the package's own
    // static declaration — nothing spawned the server. Rendering the same green
    // "Online" as a handshake-proven row asserts something no one verified.
    mockApi.mcpServers.mockResolvedValue([
      { ...server('managed'), probeMode: 'declared', probedAt: 1_700_000_000 },
    ])
    renderTab()
    await waitFor(() => expect(screen.getByText('Declared')).toBeInTheDocument())
    expect(screen.queryByText('Online')).not.toBeInTheDocument()
  })

  it('a handshake-proven server still shows "Online"', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...server('real'), probeMode: 'handshake', probedAt: 1_700_000_000 },
    ])
    renderTab()
    await waitFor(() => expect(screen.getByText('Online')).toBeInTheDocument())
    expect(screen.queryByText('Declared')).not.toBeInTheDocument()
  })
})

describe('probe-failure count', () => {
  const failing = (): McpServer => ({
    ...server('airbnb'),
    status: 'error',
    error: 'timeout after 15s',
    probeFailures: 3,
    probeFailing: true,
  })

  it('a quarantined server is labelled, alongside its real probe status', async () => {
    mockApi.mcpServers.mockResolvedValue([failing()])
    renderTab()
    await waitFor(() => expect(screen.getByText('Failing')).toBeInTheDocument())
    // The status badge is NOT replaced: "error" is still the true reading, and
    // the error detail under it is keyed on that status.
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('the label explains itself with the failure count', async () => {
    mockApi.mcpServers.mockResolvedValue([failing()])
    renderTab()
    const badge = await screen.findByText('Failing')
    expect(badge.closest('[title]')?.getAttribute('title')).toContain('3')
  })

  it('a healthy server is neither labelled nor offered a remount', async () => {
    mockApi.mcpServers.mockResolvedValue([server('alpha')])
    renderTab()
    await waitFor(() => expect(screen.getByText('Online')).toBeInTheDocument())
    expect(screen.queryByText('Failing')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Reset count/ })).not.toBeInTheDocument()
  })

  it('a failing server below the threshold is not labelled yet', async () => {
    // probeFailures without `quarantined` is the counting state. Labelling it
    // would tell the user a server was unmounted while it is still mounted.
    mockApi.mcpServers.mockResolvedValue([
      { ...server('airbnb'), status: 'error', probeFailures: 1 },
    ])
    renderTab()
    await waitFor(() => expect(screen.getByText('Error')).toBeInTheDocument())
    expect(screen.queryByText('Failing')).not.toBeInTheDocument()
  })

  it('Remount releases the server and refetches', async () => {
    mockApi.mcpServers.mockResolvedValue([failing()])
    mockApi.mcpResetProbeFailures.mockResolvedValue({ ok: true, name: 'airbnb', released: true })
    renderTab()
    const btn = await screen.findByRole('button', { name: /Reset count/ })

    // After the release the server comes back healthy — asserted through a
    // refetch rather than an optimistic local edit, because the badge must
    // disappear only if the backend really remounted it.
    mockApi.mcpServers.mockResolvedValue([server('airbnb')])
    fireEvent.click(btn)

    await waitFor(() => expect(mockApi.mcpResetProbeFailures).toHaveBeenCalledWith('airbnb'))
    await waitFor(() => expect(screen.queryByText('Failing')).not.toBeInTheDocument())
  })

  it('a failed release leaves the label in place', async () => {
    mockApi.mcpServers.mockResolvedValue([failing()])
    mockApi.mcpResetProbeFailures.mockRejectedValue(new Error('rebuild failed'))
    renderTab()
    const btn = await screen.findByRole('button', { name: /Reset count/ })
    fireEvent.click(btn)
    await waitFor(() => expect(mockApi.mcpResetProbeFailures).toHaveBeenCalled())
    // Still there: the row reflects the server, not the click.
    expect(screen.getByText('Failing')).toBeInTheDocument()
  })
})
