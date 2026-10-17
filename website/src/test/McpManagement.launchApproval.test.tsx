// MCP Management: approving the command a stubbed server will run.
//
// This page no longer shows launch commands anywhere. The operator approves by
// name; the exact-launch compare-and-set on the write is what pins the approval
// to the command the gateway resolved.
//
// Contract under test:
// - turning the STUB switch ON is ONE click: it reads the launch and, if that
//   launch can be approved, writes the stub straight away carrying its
//   expected_launch. No command is shown, and there is no review panel or second
//   button.
// - a launch that cannot be approved (incomplete display, no identity) turns
//   nothing on and says so.
// - a refused row (changed command, or added outside the dashboard) shows its
//   reason and an Approve button, and NO command, environment, before/after, or
//   "declared in" text.
// - the switch of a row waiting for approval moves the keyboard to that row's
//   Approve button rather than reading the launch again.
// - a row the panel cannot approve has no button, so its switch turns the stub off.
// - the approve action re-sends stub=true with the launch identity.
// - an older gateway that sends no `launch_refused` keeps the plain states.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { McpManagement } from '../pages/settings/McpManagement'
import { api, ApiError } from '../api/client'

const server = {
  name: 'alpha-mcp',
  stub: true,
  can_stub: true,
  in_allowlist: true,
  entry_poolable: false,
  agents: ['kirocrew'],
  transport: 'stdio',
  denylisted: false,
}

/** The same server before the operator has opted in: what the switch acts on. */
const stubbable = { ...server, stub: false }

/** What `GET /servers/launch` answers while the switch is being turned on. */
const preview = (over: Record<string, unknown> = {}) => ({
  name: 'alpha-mcp',
  commands: [['npx', '-y', 'alpha-mcp@2']],
  envs: [['TOKEN_PATH=/tmp/t']],
  complete: true,
  expected_launch: 'command-hash:env-hash',
  ...over,
})

const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  stub: ['alpha-mcp'],
  stub_count: 1,
  running: true,
  ping_ok: true,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** No command, environment, before/after, or "declared in" text renders anywhere. */
function expectNoCommandText() {
  expect(screen.queryByText('Command')).toBeNull()
  expect(screen.queryByText('Environment')).toBeNull()
  expect(screen.queryByText('Approved before')).toBeNull()
  expect(screen.queryByText('Would run now')).toBeNull()
  expect(screen.queryByText(/Declared in the config of/)).toBeNull()
  expect(screen.queryByText(/Your STUB opt-in is kept/)).toBeNull()
  expect(screen.queryByText(/Approve to let one shared backend run this exact command/)).toBeNull()
  expect(screen.queryByText(/was not recorded/)).toBeNull()
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('McpManagement launch approval', () => {
  it('shows a refused changed-command row as needing approval, with no command text', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['npx', '-y', 'alpha-mcp@2']],
            envs: [['LD_PRELOAD=/tmp/x.so', 'MODE=fast']],
            approved_commands: [['npx', '-y', 'alpha-mcp@1']],
            approved_envs: [[]],
            expected_launch: 'a:b',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('command changed', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText(/Its command changed after you approved it/)).toBeTruthy()
    expect(
      screen.getByRole('button', { name: 'Approve sharing for alpha-mcp' }),
    ).toBeTruthy()
    // Nothing from the command itself is on screen.
    expect(screen.queryByText('alpha-mcp@2')).toBeNull()
    expect(screen.queryByText('alpha-mcp@1')).toBeNull()
    expect(screen.queryByText(/LD_PRELOAD/)).toBeNull()
    expectNoCommandText()
    expect(screen.queryByText('shared', { selector: 'span' })).toBeNull()
    expect(screen.getByRole('switch', { name: 'Put a stub in front of alpha-mcp' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
  })

  it('sends the switch of a refused row to its Approve button, reading nothing', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['npx', '-y', 'alpha-mcp@2']],
            envs: [['MODE=fast']],
            approved_commands: [['npx', '-y', 'alpha-mcp@1']],
            approved_envs: [['MODE=fast']],
            expected_launch: 'a:b',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const stubSwitch = await screen.findByRole('switch', {
      name: 'Put a stub in front of alpha-mcp',
    })
    stubSwitch.click()

    const approve = await screen.findByRole('button', {
      name: 'Approve sharing for alpha-mcp',
    })
    expect(document.activeElement).toBe(approve)
    expectNoCommandText()
    expect(read).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('approves by re-sending stub=true for that name', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'added_outside_dashboard',
            commands: [['alpha']],
            envs: [[]],
            expected_launch: 'command-hash:env-hash',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true,
      name: 'alpha-mcp',
      stub: true,
      restart_required: true,
    } as never)

    mount()
    const approve = await screen.findByRole('button', {
      name: 'Approve sharing for alpha-mcp',
    })
    expect(approve.textContent).toBe('Approve sharing for alpha-mcp')
    approve.click()
    await waitFor(() =>
      expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'command-hash:env-hash'),
    )
  })

  it.each([
    [409, 'launch_changed_since_display', 'alpha-mcp: its command changed after it was read, so nothing was approved. Try again.'],
    [409, 'launch_unresolved', 'This server has no command to approve. Nothing was saved.'],
    [409, 'launch_over_cap', 'This server starts too many different commands to use a shared backend. Nothing was saved.'],
    [503, 'approval_write_failed', 'The approval could not be saved. The server keeps running inside each session.'],
  ] as const)('shows the specific approval failure for %s %s', async (statusCode, code, message) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'added_outside_dashboard',
            commands: [['alpha']],
            envs: [[]],
            expected_launch: 'command-hash:env-hash',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(statusCode, message, JSON.stringify({ error: message, code })),
    )

    mount()
    const approve = await screen.findByRole('button', {
      name: 'Approve sharing for alpha-mcp',
    })
    approve.click()
    expect(await screen.findByText(message)).toBeTruthy()
  })

  it('offers no approve control when the refusal carries no approvable identity', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': { reason: 'added_outside_dashboard', commands: [['alpha']], envs: [[]] },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('needs approval', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('Declared in a config file, and never approved here.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Approve the command/ })).toBeNull()
    expectNoCommandText()
    expect(screen.queryByText('alpha')).toBeNull()
  })

  it('turns the stub off from the switch of a row nothing here can approve', async () => {
    // With no approve button to hand the keyboard to, the switch is the only
    // control the operator has over this opt-in. It has to act.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': { reason: 'added_outside_dashboard', commands: [['alpha']], envs: [[]] },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true,
      name: 'alpha-mcp',
      stub: false,
    } as never)

    mount()
    const stubSwitch = await screen.findByRole('switch', {
      name: 'Put a stub in front of alpha-mcp',
    })
    expect(screen.queryByRole('button', { name: /Approve the command/ })).toBeNull()
    stubSwitch.click()

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined))
    // Turning off does not read the launch.
    expect(read).not.toHaveBeenCalled()
  })

  it('keeps the plain state when the gateway sends no launch_refused', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('shared', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText('needs approval', { selector: 'span' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Approve the command/ })).toBeNull()
  })
})

describe('McpManagement first stub approval (one click)', () => {
  const idle = () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ stub: [], stub_count: 0, enabled: false }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [stubbable] } as never)
  }

  const turnOn = async () => {
    mount()
    const sw = await screen.findByRole('switch', { name: 'Put a stub in front of alpha-mcp' })
    sw.click()
  }

  it('reads the launch and writes the stub in one click, with no command shown', async () => {
    idle()
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true,
      name: 'alpha-mcp',
      stub: true,
      restart_required: true,
    } as never)

    await turnOn()

    // The read gates the write; the write carries the launch identity.
    await waitFor(() =>
      expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'command-hash:env-hash'),
    )
    expect(read).toHaveBeenCalledWith('alpha-mcp')
    // No review panel, no command text.
    expect(screen.queryByRole('group', { name: 'Check what alpha-mcp will run' })).toBeNull()
    expect(screen.queryByText('alpha-mcp@2')).toBeNull()
    expect(screen.queryByText(/TOKEN_PATH/)).toBeNull()
    // The restart notice is the only outcome the operator sees.
    expect(await screen.findByText(/Restart the gateway for this to take effect/)).toBeTruthy()
  })

  it('does not turn the stub on when the launch cannot be approved', async () => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      preview({ complete: false, expected_launch: undefined }) as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    await turnOn()

    // The one-line text says it cannot be approved; nothing is written.
    expect(await screen.findByText(/cannot be approved for sharing/)).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
    expect(screen.queryByText('alpha-mcp@2')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Put a stub in front of alpha-mcp' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
  })

  it.each([
    ['launch_unresolved', 'alpha-mcp has no command to show, so it cannot use a shared backend. Nothing was turned on.'],
    ['launch_over_cap', 'alpha-mcp starts too many different commands to use a shared backend. Nothing was turned on.'],
    ['invalid_server_name', 'alpha-mcp is not a name Kiro Crew can read as a server. Nothing was turned on.'],
    ['launch_resolve_failed', 'The command alpha-mcp would run could not be read. Nothing was turned on.'],
  ] as const)('says nothing was turned on when the read fails with %s', async (code, message) => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(
      new ApiError(409, code, JSON.stringify({ error: code, code })),
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    await turnOn()

    expect(await screen.findByText(message)).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it.each([
    [409, 'expected_launch_required', /command could not be checked/],
    [409, 'launch_display_incomplete', /too long to show in full/],
    [400, 'batch_stub_requires_individual', /approved from its own command/],
  ] as const)('reports the write refusal %s %s in its own words', async (statusCode, code, matcher) => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(statusCode, code, JSON.stringify({ error: code, code })),
    )

    // One click reads the launch and fires the write, which is refused.
    await turnOn()

    expect(await screen.findByText(matcher)).toBeTruthy()
  })
})

describe('McpManagement changed-command row shows no command', () => {
  const changed = (over: Record<string, unknown> = {}) =>
    status({
      launch_refused: {
        'alpha-mcp': {
          reason: 'changed_needs_reapproval',
          commands: [['npx', '-y', 'alpha-mcp@3']],
          envs: [['MODE=fast']],
          approved_commands: [['npx', '-y', 'alpha-mcp@2']],
          approved_envs: [[]],
          complete: true,
          expected_launch: 'a:b',
          ...over,
        },
      },
    })

  it('renders no before/after, command or environment text', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(changed() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    // The reason and the Approve button are all that is on the sub-row.
    expect(await screen.findByText(/Its command changed after you approved it/)).toBeTruthy()
    expect(
      screen.getByRole('button', { name: 'Approve sharing for alpha-mcp' }),
    ).toBeTruthy()
    expect(screen.queryByText('alpha-mcp@3')).toBeNull()
    expect(screen.queryByText('alpha-mcp@2')).toBeNull()
    expectNoCommandText()
  })

  it('puts the reason in a full-width sub-row, with the pill still in the state column', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(changed() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    const reason = await screen.findByText(/Its command changed after you approved it/)
    const cell = reason.closest('td')
    expect(cell?.getAttribute('colspan')).toBe('4')
    // The pill stays where a column value belongs.
    expect(screen.getByText('command changed', { selector: 'span' }).closest('td')).not.toBe(cell)
  })
})
