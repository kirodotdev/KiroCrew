import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
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
// The path chip copies through the shared util; `true` = the text reached the
// clipboard, which is what flips the chip to its "Copied" state.
const mockCopy = vi.hoisted(() => vi.fn().mockResolvedValue({ ok: true, hadAsyncApi: true }))
vi.mock('../utils/clipboard', () => ({ copyWithOutcome: mockCopy }))

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
  // A config with no `connections_ui` key is the shipped default, and it now
  // resolves the gate ON — so the in-place sign-in is available to any managed
  // row unless a test pulls the escape hatch with an explicit false.
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

  it('makes the unverifiable status explanation keyboard and touch reachable', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    const badge = await screen.findByText('Not verified')
    const trigger = within(badge.parentElement!).getByRole('button', { name: 'More information' })
    const hint = trigger.getAttribute('title') || ''
    // Says who holds the token and that a working server is still working —
    // the two facts that make the badge honest instead of alarming.
    expect(hint).toContain('atlassian')
    expect(hint).toContain('Kiro CLI')
    expect(hint).toMatch(/cannot see the authorization/)
    expect(badge).not.toHaveAttribute('title')

    fireEvent.click(trigger)
    expect(await screen.findByText(/cannot see the authorization/i)).toBeInTheDocument()
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
   * validity is the one thing it cannot check, and the InfoTip says so rather than
   * the badge over-claiming. The explanation rides InfoTip rather than a native
   * `title` for the same reason as the `not_verified` case above: a hover-only
   * tooltip is unreachable by keyboard, touch, and AT.
   */
  it('reports a held runtime grant as signed in, without claiming it still works', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...remote('needs_auth'), authChallenge: true, authGrantPresent: true },
    ])
    renderTab()

    const badge = await screen.findByText('Signed in')
    expect(screen.queryByText('Sign-in required')).not.toBeInTheDocument()
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
    // Muted, and deliberately neither of the other two tones. Amber is this panel's
    // "you need to act" colour, so a resolved row wearing it is indistinguishable by
    // colour from the one still asking to be signed in; green would claim the server
    // answers, which the probe cannot check without the runtime's token.
    expect(badge.className).toContain('text-[var(--muted)]')
    expect(badge.className).not.toContain('text-warn')
    expect(badge.className).not.toContain('text-ok')
    expect(badge).not.toHaveAttribute('title')

    const trigger = within(badge.parentElement!).getByRole('button', { name: 'More information' })
    expect(trigger.getAttribute('title') || '').toMatch(/cannot confirm the sign-in is still valid/)

    fireEvent.click(trigger)
    expect(await screen.findByText(/cannot confirm the sign-in is still valid/i)).toBeInTheDocument()
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

    const badge = await screen.findByText('Sign-in required')
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
    // The short clause is VISIBLE in the cell, because a `title` reaches neither a
    // keyboard nor a touch user — and the panel serves from the probe cache for the
    // whole TTL, so someone returning from a completed sign-in would meet a row
    // still reading "Sign-in required" and conclude it had failed. That one clause
    // rides the cell; the longer form, naming the control and the resulting state,
    // rides the InfoTip rather than a hover-only `title` for the same reachability
    // reason as the `not_verified` and `signed_in` cases above.
    expect(screen.getByText(/Then probe to refresh this list/)).toBeInTheDocument()
    expect(badge).not.toHaveAttribute('title')

    const trigger = within(badge.parentElement!).getByRole('button', { name: 'More information' })
    expect(trigger.getAttribute('title') || '').toMatch(
      /use the Probe MCP servers button above this table; this row will then read Signed in/,
    )

    fireEvent.click(trigger)
    expect(
      await screen.findByText(/use the Probe MCP servers button above this table; this row will then read Signed in/i),
    ).toBeInTheDocument()

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
   * sign-in in place, reusing the headless mint engine — but only while the
   * Connections UI is on, which is now the default. A non-resolvable row, or an
   * instance that pulled the `connections_ui: false` escape hatch, keeps the chat
   * prose unchanged — minting is never offered for arbitrary URLs (parked
   * maintainer decision #4286), and with no cards on screen chat is again the
   * only authorize prompt.
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

  it('offers the in-place Sign in with no connections_ui key at all — the shipped default', async () => {
    // The launch flip reaches this surface too: an install that never set the
    // flag gets the same in-place sign-in an explicit `true` gets. `beforeEach`
    // already mocks a config with no Connections key, so this test deliberately
    // does not override it.
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
    await waitFor(() => expect(screen.getByRole('button', { name: /Sign in/ })).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: /Go to chat/ })).not.toBeInTheDocument()
  })

  it('FIX 1: falls back to the chat prose on a resolvable managed row when connections_ui is explicitly false', async () => {
    // The escape hatch has to reach the mint engine, not just the gallery: with
    // `connections_ui: false` even a registry-resolvable row must show the same
    // chat guidance a non-registry row does, because that instance has no cards
    // and chat is once again the only authorize prompt.
    mockApi.kirocrewConfig.mockResolvedValue({ connections_ui: false })
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

/**
 * A server marked disabled in the shared Kiro MCP config stays in this table
 * through a sync or probe, as the Kiro IDE keeps it: a greyed "Disabled" row.
 * This suite pins how the table shows it — present, greyed, badged, its scope
 * toggles inert, one line saying which file to edit, Uninstall still live and
 * honest about what it does — and that nothing changes for enabled rows or for
 * Kiro Crew's own consent-disabled rows (#13075). Which of the two disabled
 * states a row is in comes from the backend's `disabledIn`, never from
 * `enabled` + `kirocrewManaged`.
 */
describe('McpTab disabled-in-config rows', () => {
  const SHARED_FILE = '~/.kiro/settings/mcp.json'
  const disabledInConfig = (over: Partial<McpServer> = {}): McpServer => ({
    ...server('figma'),
    status: 'disabled',
    enabled: false,
    kirocrewManaged: false,
    disabledIn: 'shared',
    disabledInFile: SHARED_FILE,
    tools: [],
    ...over,
  })
  const consentDisabled = (): McpServer => ({
    ...server('weather'),
    status: 'disabled',
    enabled: false,
    kirocrewManaged: true,
    disabledIn: 'kirocrew',
    disabledInFile: null,
    tools: [],
  })
  const row = (name: string): HTMLElement => {
    const tr = screen.getByText(name, { selector: 'code' }).closest('tr')
    if (!tr) throw new Error(`no row for ${name}`)
    return tr
  }

  it('keeps the disabled server in the list, greyed and badged, with the header count', async () => {
    mockApi.mcpServers.mockResolvedValue([server('alpha'), disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    // Same list as the enabled rows, not a separate section.
    expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument()
    expect(screen.getByTestId('mcp-disabled-count')).toHaveTextContent('1 disabled')
    const tr = row('figma')
    expect(tr).toHaveAttribute('data-disabled-in-config', 'true')
    // Greyed per CELL, not per row: Uninstall stays live on this row, and a
    // dimmed live button reads as disabled. The five non-actionable cells dim;
    // the <tr> and the sticky actions cell keep full opacity.
    expect(tr).toHaveStyle({ opacity: '1' })
    const cells = Array.from(tr.querySelectorAll('td'))
    expect(cells).toHaveLength(6)
    for (const cell of cells.slice(0, 5)) expect(cell.style.opacity).toBe('0.6')
    expect(cells[5].style.opacity).toBe('')
    expect(within(cells[5]).getByRole('button', { name: 'Uninstall' })).toBeEnabled()
    const badge = within(tr).getByText('Disabled in config')
    // A deliberate disable is an off switch: the muted tone, never the error
    // tone the invalid-value row wears; and no "Config error" chip.
    expect(badge.className).toContain('var(--muted)')
    expect(badge.className).not.toContain('text-danger')
    expect(within(tr).queryByTestId('mcp-config-error-chip')).not.toBeInTheDocument()
    // Inside a dimmed cell the muted token fell under contrast in the light
    // themes; the chip's text sits one token step up.
    expect(badge.getAttribute('style')).toContain('var(--text)')
    // The badge carries the explanation the two words cannot: which file, and
    // that nothing here can change it.
    expect(badge.closest('[title]')?.getAttribute('title')).toMatch(/shared Kiro MCP config/)
    const status = within(tr).getByText('Disabled')
    expect(status).toBeInTheDocument()
    expect(status.className).toContain('var(--muted)')
  })

  it('says on the row which file disabled it and where to turn it back on', async () => {
    // In words on the row, not only in hover text: a keyboard or touch user
    // never sees a `title`, and this row's whole point is to say why it is grey.
    mockApi.mcpServers.mockResolvedValue([disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const where = within(row('figma')).getByTestId('mcp-disabled-in-config-where')
    // Honest about the missing affordance: this table has no re-enable switch
    // (a separate decision), so the line says editing the file is the way and
    // that a switch is planned -- and hands over the one thing it can, the path,
    // as a chip that copies it.
    expect(where).toHaveTextContent(`Disabled in ${SHARED_FILE}. Editing this file lifts its disable; another config may still keep the server off. A switch is planned.`)
    expect(where).not.toHaveTextContent('To turn it back on')
    const chip = within(where).getByTestId('mcp-disabled-in-config-path')
    expect(chip).toHaveTextContent(SHARED_FILE)
    expect(chip).toHaveAttribute('aria-label', `Copy the path ${SHARED_FILE}`)
    expect(chip.getAttribute('title')).toBe('Copy path')
    fireEvent.click(chip)
    expect(mockCopy).toHaveBeenCalledWith(SHARED_FILE)
    await waitFor(() => expect(chip).toHaveAttribute('data-copied', 'true'))
    expect(chip.getAttribute('title')).toBe('Copied')
  })

  it('names the shared MCP config when the backend cannot name the file', async () => {
    mockApi.mcpServers.mockResolvedValue([disabledInConfig({ disabledInFile: null })])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const where = within(row('figma')).getByTestId('mcp-disabled-in-config-where')
    expect(where).toHaveTextContent('Disabled in the shared MCP config. Editing that config lifts its disable; another config may still keep the server off. A switch is planned.')
    expect(where).not.toHaveTextContent('~/')
    // No path to copy, so no chip.
    expect(within(where).queryByTestId('mcp-disabled-in-config-path')).not.toBeInTheDocument()
  })

  it('reports a refused copy in place through ErrorNotice with the hand-off, never as Copied', async () => {
    // `copyWithOutcome` reports the clipboard refusing: no async API (a plain-HTTP
    // gateway) or a denied permission. That outcome is an error the user must
    // see and the agent can act on: it renders through `ErrorNotice` (the repo's
    // one error surface) WITH the standard hand-off, backed by a journaled report
    // whose code names the failure and whose detail is the raw reason. The chip
    // stays out of its "Copied" state. Red under the silent `if (!ok) return`,
    // and red again under a notice without `askAgent`.
    mockCopy.mockResolvedValueOnce({ ok: false, hadAsyncApi: false })
    mockApi.mcpServers.mockResolvedValue([disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const where = within(row('figma')).getByTestId('mcp-disabled-in-config-where')
    const chip = within(where).getByTestId('mcp-disabled-in-config-path')
    fireEvent.click(chip)
    const notice = await within(where).findByTestId('mcp-disabled-in-config-path-copy-failed')
    expect(notice).toHaveTextContent('Copy failed')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(within(notice).getByText('Ask the agent')).toBeInTheDocument()
    // Stacked on its own line (the notice's block shape), never inline beside the
    // chip: the sentence's row holds the chip, the notice holds Ask the agent +
    // Dismiss -- no row carries more than two actions.
    expect(notice.tagName).toBe('DIV')
    expect(within(notice).getAllByRole('button').length).toBeLessThanOrEqual(2)
    expect(notice.previousElementSibling === chip || notice.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
    expect(chip).not.toHaveAttribute('data-copied')
    expect(chip.getAttribute('title')).toBe('Copy path')
    // The journal holds the structured report the hand-off carries.
    const { recentErrors } = await import('../utils/errorReport')
    const report = recentErrors().find(r => r.message === 'Copy failed')
    expect(report?.code).toBe('clipboard_unavailable')
    expect(report?.source).toBe('system')
    expect(report?.detail).toBeUndefined()
    // A denied permission is told apart from an absent API.
    mockCopy.mockResolvedValueOnce({ ok: false, hadAsyncApi: true, asyncError: Object.assign(new Error('denied'), { name: 'NotAllowedError' }) })
    fireEvent.click(chip)
    await waitFor(() => expect(recentErrors().some(r => r.code === 'clipboard_denied' && r.detail?.includes('denied'))).toBe(true))
    // Dismissable.
    fireEvent.click(within(within(where).getByTestId('mcp-disabled-in-config-path-copy-failed')).getByRole('button', { name: /dismiss|close/i }))
    expect(within(where).queryByTestId('mcp-disabled-in-config-path-copy-failed')).not.toBeInTheDocument()
  })

  it('names the invalid value, not a switch, when the backend read "disabled" fail-closed', async () => {
    // `"disabled": "false"` (a string) is read fail-closed by the backend -- an
    // invalid value never launches a server -- and "set it to false" would be
    // the wrong instruction for a value that already reads "false". The row
    // says what is wrong and where, and it reads as an ERROR to repair, not as
    // an off switch: a "Config error" chip and the status badge in the error
    // tone, and no greying. The controls stay inert as for any shared-config
    // disable -- the fix lives in the shared file.
    mockApi.mcpServers.mockResolvedValue([disabledInConfig({ disabledReason: 'invalid' })])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const tr = row('figma')
    const where = within(tr).getByTestId('mcp-disabled-in-config-where')
    expect(where).toHaveTextContent(`Disabled: invalid "disabled" value in ${SHARED_FILE}. Set it to true or false there.`)
    expect(where).not.toHaveTextContent('To turn it back on')
    expect(where).toHaveAttribute('data-disabled-reason', 'invalid')
    expect(tr).toHaveAttribute('data-disabled-in-config', 'true')
    // The chip: "Config error" in the error tone, never the muted "Disabled in
    // config" of a deliberate disable; its help is the repair line itself.
    expect(within(tr).queryByText('Disabled in config')).not.toBeInTheDocument()
    const chip = within(tr).getByTestId('mcp-config-error-chip')
    expect(chip).toHaveTextContent('Config error')
    expect(chip.className).toContain('text-danger')
    expect(chip.className).not.toContain('var(--muted)')
    expect(chip.getAttribute('title')).toBe(`Disabled: invalid "disabled" value in ${SHARED_FILE}. Set it to true or false there.`)
    // The repair line names the file as the same copyable chip.
    expect(within(where).getByTestId('mcp-disabled-in-config-path')).toHaveTextContent(SHARED_FILE)
    // The status badge says "Invalid value" in the error tone -- words, not only
    // colour and the chip, tell it apart from a deliberate "Disabled".
    expect(within(tr).queryByText('Disabled')).not.toBeInTheDocument()
    const status = within(tr).getByText('Invalid value')
    expect(status.className).toContain('text-danger')
    expect(status.className).not.toContain('var(--muted)')
    // Not greyed: every cell keeps full opacity, so the row does not read as off.
    for (const cell of Array.from(tr.querySelectorAll('td'))) expect(cell.style.opacity).toBe('')
    // Still inert -- the panel cannot repair the value.
    for (const scope of ['kirocrew', 'kiroGlobal']) expect(tr.querySelector(`button[data-scope="${scope}"]`)).toBeDisabled()
  })

  it('names the shared MCP config for an invalid value when the backend cannot name the file', async () => {
    mockApi.mcpServers.mockResolvedValue([disabledInConfig({ disabledReason: 'invalid', disabledInFile: null })])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const where = within(row('figma')).getByTestId('mcp-disabled-in-config-where')
    expect(where).toHaveTextContent('Disabled: invalid "disabled" value in the shared MCP config. Set it to true or false there.')
    expect(where).not.toHaveTextContent('~/')
  })

  it('keeps the switch wording for a real disable, whatever else the row carries', async () => {
    // `disabledReason: null` is the backend's "a real true is the story".
    mockApi.mcpServers.mockResolvedValue([disabledInConfig({ disabledReason: null })])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const where = within(row('figma')).getByTestId('mcp-disabled-in-config-where')
    expect(where).toHaveTextContent(`Disabled in ${SHARED_FILE}. Editing this file lifts its disable; another config may still keep the server off. A switch is planned.`)
    expect(where).not.toHaveTextContent('invalid')
    expect(where).not.toHaveAttribute('data-disabled-reason')
    expect(within(row('figma')).getByText('Disabled')).toBeInTheDocument()
    expect(within(row('figma')).queryByText('Invalid value')).not.toBeInTheDocument()
  })

  it('renders the scope toggles inert and Uninstall live and honest', async () => {
    mockApi.mcpServers.mockResolvedValue([disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const tr = row('figma')
    // Uninstall still purges the name from every config, the shared file
    // included, so the button stays live and its hover text says exactly that
    // rather than a greyed button implying it would do nothing.
    const uninstall = within(tr).getByRole('button', { name: 'Uninstall' })
    expect(uninstall).toBeEnabled()
    expect(uninstall.getAttribute('title')).toBe('Removes this server from every MCP config, including the shared one it is disabled in.')
    // Both scope badges are inert and say why -- and each keeps its OWN scope
    // name in its title/aria-label. Under the label-free help sentence both
    // badges read identically to a screen reader: two unnamed inert buttons.
    // Not the pending-uninstall wording the same styling means on a staged row.
    const names: string[] = []
    for (const [scope, label] of [['kirocrew', 'Kiro Crew'], ['kiroGlobal', 'Kiro']] as const) {
      const badge = tr.querySelector<HTMLButtonElement>(`button[data-scope="${scope}"]`)
      expect(badge, scope).not.toBeNull()
      expect(badge).toBeDisabled()
      const title = badge?.getAttribute('title') ?? ''
      expect(title).toBe(`${label}: off (disabled in the shared MCP config; change it there)`)
      expect(badge?.getAttribute('aria-label')).toBe(title)
      expect(title).not.toMatch(/pending uninstall/)
      names.push(title)
    }
    expect(new Set(names).size).toBe(names.length)
    // No re-enable control exists in this table for it (a separate decision).
    expect(within(tr).queryByRole('button', { name: /Enable/ })).not.toBeInTheDocument()
    // And the live Uninstall does what it says: it stages the removal.
    fireEvent.click(uninstall)
    await waitFor(() => expect(screen.getByText(/1 pending change/)).toBeInTheDocument())
    expect(within(row('figma')).getByRole('button', { name: 'Undo' })).toBeInTheDocument()
  })

  it('renders a store-managed server that the shared config disables inert, not as a consent row', async () => {
    // The dual-scope corner: the entry is in Kiro Crew's store (so the row is
    // `kirocrewManaged`) but the SHARED config carries the disable. Offering the
    // consent step here would offer a re-enable that cannot land — lifting the
    // store's flag leaves the shared one — so the backend's `disabledIn`
    // decides, not `kirocrewManaged`.
    mockApi.mcpServers.mockResolvedValue([disabledInConfig({ kirocrewManaged: true })])
    renderTab()
    await waitFor(() => expect(screen.getByText('figma', { selector: 'code' })).toBeInTheDocument())
    const tr = row('figma')
    expect(tr).toHaveAttribute('data-disabled-in-config', 'true')
    expect(within(tr).getByText('Disabled in config')).toBeInTheDocument()
    expect(within(tr).getByTestId('mcp-disabled-in-config-where')).toBeInTheDocument()
    expect(tr.querySelector('button[data-scope="kirocrew"]')).toBeDisabled()
    // Still the store's entry: the JSON editor keeps its action.
    expect(within(tr).getByRole('button', { name: 'Edit JSON for figma' })).toBeEnabled()
  })

  it('shows no count when nothing is disabled', async () => {
    mockApi.mcpServers.mockResolvedValue([server('alpha'), server('beta')])
    renderTab()
    await waitFor(() => expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument())
    expect(screen.queryByTestId('mcp-disabled-count')).not.toBeInTheDocument()
    expect(screen.queryByText('Disabled in config')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mcp-disabled-in-config-where')).not.toBeInTheDocument()
  })

  it('leaves enabled rows exactly as before', async () => {
    mockApi.mcpServers.mockResolvedValue([server('alpha'), disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('alpha', { selector: 'code' })).toBeInTheDocument())
    const tr = row('alpha')
    expect(tr).not.toHaveAttribute('data-disabled-in-config')
    expect(tr).toHaveStyle({ opacity: '1' })
    for (const cell of Array.from(tr.querySelectorAll('td'))) expect(cell.style.opacity).toBe('')
    expect(within(tr).queryByText('Disabled in config')).not.toBeInTheDocument()
    expect(within(tr).queryByTestId('mcp-disabled-in-config-where')).not.toBeInTheDocument()
    const uninstall = within(tr).getByRole('button', { name: 'Uninstall' })
    expect(uninstall).toBeEnabled()
    expect(uninstall).not.toHaveAttribute('title')
    expect(tr.querySelector('button[data-scope="kirocrew"]')).toBeEnabled()
    expect(within(tr).getByText('Online')).toBeInTheDocument()
  })

  it('keeps the controls on a consent-disabled row from Kiro Crew\u2019s own store', async () => {
    // A registry install or custom add lands disabled in the Kiro Crew scope
    // until the user enables it — through the Kiro Crew badge + Apply, in THIS
    // table. That row is disabled too, and counts as such, but it is not
    // "disabled in config" and must keep every control it had.
    mockApi.mcpServers.mockResolvedValue([consentDisabled(), disabledInConfig()])
    renderTab()
    await waitFor(() => expect(screen.getByText('weather', { selector: 'code' })).toBeInTheDocument())
    expect(screen.getByTestId('mcp-disabled-count')).toHaveTextContent('2 disabled')
    const tr = row('weather')
    expect(tr).not.toHaveAttribute('data-disabled-in-config')
    expect(within(tr).queryByText('Disabled in config')).not.toBeInTheDocument()
    expect(within(tr).queryByTestId('mcp-disabled-in-config-where')).not.toBeInTheDocument()
    expect(within(tr).getByRole('button', { name: 'Uninstall' })).toBeEnabled()
    const kirocrew = tr.querySelector<HTMLButtonElement>('button[data-scope="kirocrew"]')
    expect(kirocrew).toBeEnabled()
    // The consent step still stages a pending change.
    fireEvent.click(kirocrew!)
    await waitFor(() => expect(screen.getByText(/1 pending change/)).toBeInTheDocument())
  })
})
