// MCP Management: approving the command a stubbed server will run.
//
// Contract under test:
// - turning the STUB switch ON writes NOTHING by itself: it reads the launch and
//   shows the command, and only the review's own button approves it
// - the approval carries the identity of the launch that was on screen, so a
//   command that moves between the render and the click is refused, not approved
// - a launch that could not be shown in full offers no approval at all, and says
//   so rather than leaving a button that cannot work
// - a CHANGED command shows what was approved before beside what would run now
// - the switch of a row waiting for approval keeps that comparison on screen and
//   hands the keyboard to its approve button, rather than reading the launch again
//   and replacing the before/after with a single-command review panel
// - a stub listed in `launch_refused` reads `needs approval`, never `stub` or
//   `shared`, because the gateway will not run it until the operator approves
// - the row shows every exact command and declared environment the approval
//   covers, so the operator approves launch content and not a name
// - the approve action re-sends stub=true with the identity of the launch shown,
//   and does not turn the stub off the way the row's switch would
// - an older gateway that sends no `launch_refused` keeps the plain states
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, within } from '@testing-library/react'
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

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('McpManagement launch approval', () => {
  it('shows a refused launch as needing approval, with its command and environment', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['npx', '-y', 'alpha-mcp@2']],
            envs: [['LD_PRELOAD=/tmp/x.so', 'MODE=fast']],
            expected_launch: 'a:b',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('command changed', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('Declared in the config of: kirocrew.')).toBeTruthy()
    expect(screen.getByText(/Your STUB opt-in is kept/)).toBeTruthy()
    expect(screen.getByText('Command')).toBeTruthy()
    expect(screen.getByText('alpha-mcp@2')).toBeTruthy()
    expect(screen.getByText('Environment')).toBeTruthy()
    expect(screen.getByText(/LD_PRELOAD=\/tmp\/x\.so/)).toBeTruthy()
    expect(screen.getByText(/Its command changed after you approved it/)).toBeTruthy()
    expect(screen.getByText(/Approve to let one shared backend run this exact command/)).toBeTruthy()
    expect(screen.queryByText('shared', { selector: 'span' })).toBeNull()
    expect(screen.getByRole('switch', { name: 'Put a stub in front of alpha-mcp' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
  })

  it('sends the switch of a refused row to the approve button it already shows', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'added_outside_dashboard',
            commands: [['old-command']],
            envs: [[]],
            expected_launch: 'old:launch',
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
    expect(stubSwitch).toHaveAttribute('aria-checked', 'false')
    stubSwitch.click()

    const approve = await screen.findByRole('button', {
      name: 'Approve the command alpha-mcp will run',
    })
    expect(document.activeElement).toBe(approve)
    // The panel on screen is still the refusal, so the command it was refused for
    // is still readable. The review panel would have replaced it.
    expect(screen.getByText('old-command')).toBeTruthy()
    expect(screen.queryByRole('group', { name: 'Check what alpha-mcp will run' })).toBeNull()
    expect(read).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('keeps the before/after on screen when a changed row’s switch is clicked', async () => {
    // The switch must not start a launch preview here: that swaps the refusal panel
    // for the review panel, so "Approved before" and "Would run now" vanish at the
    // exact moment the operator is deciding between them.
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
      name: 'Approve the command alpha-mcp will run',
    })
    expect(document.activeElement).toBe(approve)
    expect(screen.getByText('Approved before')).toBeTruthy()
    expect(screen.getByText('Would run now')).toBeTruthy()
    expect(screen.getByText('alpha-mcp@1')).toBeTruthy()
    expect(screen.getByText('alpha-mcp@2')).toBeTruthy()
    expect(read).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
    expect(
      screen.queryByText('Reading the command alpha-mcp would run…'),
    ).toBeNull()
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
      name: 'Approve the command alpha-mcp will run',
    })
    expect(approve.className).toContain('font-body')
    expect(approve.textContent).toBe("Approve and share alpha-mcp's command")
    approve.click()
    await waitFor(() =>
      expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'command-hash:env-hash'),
    )
  })

  it.each([
    [409, 'launch_changed_since_display', 'alpha-mcp: the command changed after it was shown, so nothing was approved. The command below is the one that would run now. Check it before you approve.'],
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
      name: 'Approve the command alpha-mcp will run',
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
    expect(screen.getByText('Command')).toBeTruthy()
    expect(screen.queryByText('Environment')).toBeNull()
    expect(screen.queryByRole('button', { name: /Approve the command/ })).toBeNull()
    // The reason line must not send the operator to a control that is not there.
    expect(screen.queryByText(/then approve it/)).toBeNull()
    expect(screen.getByText(/too long for Kiro Crew to show in full/)).toBeTruthy()
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
    // Reading the launch would swap the refusal panel for the review one, which
    // is the behaviour a row WITH an approve button is pinned to above.
    expect(read).not.toHaveBeenCalled()
  })

  it('keeps argument and environment element boundaries visible', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ stub: [], stub_count: 0, enabled: false }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [stubbable] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      preview({ commands: [['a b', 'c']], envs: [['TOKEN=line one\nline two']] }) as never,
    )

    mount()
    const stubSwitch = await screen.findByRole('switch', {
      name: 'Put a stub in front of alpha-mcp',
    })
    stubSwitch.click()

    const command = await screen.findByRole('list', { name: 'Command' })
    expect(within(command).getAllByRole('listitem').map(item => item.textContent)).toEqual(['a b', 'c'])
    const environment = screen.getByRole('list', { name: 'Environment' })
    expect(within(environment).getAllByRole('listitem')).toHaveLength(1)
    expect(within(environment).getByRole('listitem').textContent).toBe('TOKEN=line one\\nline two')
  })

  it('renders every invisible character as a literal escape', async () => {
    // One argument per hazard the C category covers: a bidi override that
    // reorders the line, and a soft hyphen that leaves no mark at all. An
    // ordinary argument with a space in it is not touched.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ stub: [], stub_count: 0, enabled: false }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [stubbable] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      preview({
        commands: [['safe\u202edanger', 'soft\u00adhyphen', 'word\u2060joiner', 'plain arg']],
      }) as never,
    )

    mount()
    const stubSwitch = await screen.findByRole('switch', {
      name: 'Put a stub in front of alpha-mcp',
    })
    stubSwitch.click()

    const command = await screen.findByRole('list', { name: 'Command' })
    expect(within(command).getAllByRole('listitem').map(item => item.textContent)).toEqual([
      'safe\\u202edanger',
      'soft\\xadhyphen',
      'word\\u2060joiner',
      'plain arg',
    ])
  })

  it('shows every command one approval covers when a name resolves to several', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'added_outside_dashboard',
            commands: [
              ['alpha', '--one'],
              ['alpha', '--two'],
            ],
            envs: [[], []],
            expected_launch: 'a:b,c:d',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('--one')).toBeTruthy()
    expect(screen.getByText('--two')).toBeTruthy()
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

describe('McpManagement first stub approval', () => {
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

  it('shows the command first and writes only what it showed', async () => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true,
      name: 'alpha-mcp',
      stub: true,
      restart_required: true,
    } as never)

    await turnOn()

    // The command, its environment, and the consequence — before any write.
    expect(await screen.findByText('alpha-mcp@2')).toBeTruthy()
    expect(screen.getByText(/TOKEN_PATH=\/tmp\/t/)).toBeTruthy()
    expect(screen.getByText(/Approve to let one shared backend run this exact command/)).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()

    // Full width, not the 16% STATE column: a command rendered one word per line
    // is not a command anyone can check.
    expect(screen.getByText('alpha-mcp@2').closest('td')?.getAttribute('colspan')).toBe('4')
    // Focus is on the decision, not still on the switch that opened it.
    const panel = screen.getByRole('group', { name: 'Check what alpha-mcp will run' })
    expect(document.activeElement).toBe(panel)

    screen.getByRole('button', { name: 'Approve and share the command alpha-mcp will run' }).click()
    await waitFor(() =>
      expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'command-hash:env-hash'),
    )
    // The panel closes once its question is answered.
    await waitFor(() => expect(screen.queryByText('alpha-mcp@2')).toBeNull())
  })

  it('writes nothing when the review is cancelled', async () => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    await turnOn()
    ;(await screen.findByRole('button', { name: 'Leave alpha-mcp as it is' })).click()

    await waitFor(() => expect(screen.queryByText('alpha-mcp@2')).toBeNull())
    expect(setStub).not.toHaveBeenCalled()
  })

  it('says the switch waits for the approval, and hands focus back on cancel', async () => {
    const { fireEvent } = await import('@testing-library/react')
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)

    mount()
    const sw = await screen.findByRole('switch', { name: 'Put a stub in front of alpha-mcp' })
    sw.focus()
    sw.click()

    expect(await screen.findByText('The switch turns on once you approve.')).toBeTruthy()
    const panel = screen.getByRole('group', { name: 'Check what alpha-mcp will run' })
    expect(document.activeElement).toBe(panel)

    fireEvent.keyDown(panel, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('alpha-mcp@2')).toBeNull())
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('switch', { name: 'Put a stub in front of alpha-mcp' }),
      ),
    )
  })

  it('closes the review on Escape, still writing nothing', async () => {
    const { fireEvent } = await import('@testing-library/react')
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    await turnOn()
    const panel = await screen.findByRole('group', { name: 'Check what alpha-mcp will run' })
    fireEvent.keyDown(panel, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByText('alpha-mcp@2')).toBeNull())
    expect(setStub).not.toHaveBeenCalled()
  })

  it('offers no approval when the launch could not be shown in full', async () => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      preview({ complete: false, expected_launch: undefined }) as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    await turnOn()

    // What was read is still shown — hiding it would leave the operator with a
    // refusal and no way to see what it was about.
    expect(await screen.findByText('alpha-mcp@2')).toBeTruthy()
    expect(screen.getByText(/too long for Kiro Crew to show in full/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Approve and share/ })).toBeNull()
    // Cancel remains, so the panel is not a dead end.
    expect(screen.getByRole('button', { name: 'Leave alpha-mcp as it is' })).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
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

    // "Nothing was saved" would be a claim about a write that never ran.
    expect(await screen.findByText(message)).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it.each([
    [409, 'expected_launch_required', /can only be shared from the command shown to you/],
    [409, 'launch_display_incomplete', /too long to show in full/],
    [400, 'batch_stub_requires_individual', /approved from its own command/],
  ] as const)('reports the write refusal %s %s in its own words', async (statusCode, code, matcher) => {
    idle()
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview() as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(statusCode, code, JSON.stringify({ error: code, code })),
    )

    await turnOn()
    ;(
      await screen.findByRole('button', {
        name: 'Approve and share the command alpha-mcp will run',
      })
    ).click()

    expect(await screen.findByText(matcher)).toBeTruthy()
  })

  it('re-reads the launch when the command moved after it was shown', async () => {
    idle()
    const read = vi
      .spyOn(api, 'mcpGatewayLaunchPreview')
      .mockResolvedValueOnce(preview() as never)
      .mockResolvedValue(
        preview({ commands: [['npx', '-y', 'alpha-mcp@3']], expected_launch: 'new:new' }) as never,
      )
    vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(
        409,
        'launch_changed_since_display',
        JSON.stringify({ error: 'changed', code: 'launch_changed_since_display' }),
      ),
    )

    await turnOn()
    ;(
      await screen.findByRole('button', {
        name: 'Approve and share the command alpha-mcp will run',
      })
    ).click()

    // The error says to check the command below; the command below must be the
    // one that would run NOW, not the stale display the write was refused for.
    expect(await screen.findByText('alpha-mcp@3')).toBeTruthy()
    expect(read).toHaveBeenCalledTimes(2)
  })
})

describe('McpManagement changed-command comparison', () => {
  const changed = (over: Record<string, unknown> = {}) =>
    status({
      launch_refused: {
        'alpha-mcp': {
          reason: 'changed_needs_reapproval',
          commands: [['npx', '-y', 'alpha-mcp@3']],
          envs: [['MODE=fast']],
          complete: true,
          expected_launch: 'a:b',
          ...over,
        },
      },
    })

  it('shows what was approved before beside what would run now', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      changed({
        approved_commands: [['npx', '-y', 'alpha-mcp@2']],
        approved_envs: [[]],
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('Approved before')).toBeTruthy()
    expect(screen.getByText('alpha-mcp@2')).toBeTruthy()
    expect(screen.getByText('Would run now')).toBeTruthy()
    expect(screen.getByText('alpha-mcp@3')).toBeTruthy()
    // The env belongs to the launch it was recorded with: the approved one had
    // none, so only the current launch shows one.
    expect(screen.getAllByText('Environment')).toHaveLength(1)
    expect(screen.queryByText(/was not recorded/)).toBeNull()
  })

  it('says the earlier command was not recorded rather than showing an empty one', async () => {
    // An approval written before the record kept its content. An empty
    // before-state would read as "you approved nothing", which is a different
    // and false claim.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(changed() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    expect(await screen.findByText('Approved before')).toBeTruthy()
    expect(
      screen.getByText('The command you approved earlier was not recorded, so there is nothing to compare.'),
    ).toBeTruthy()
    expect(screen.getByText('alpha-mcp@3')).toBeTruthy()
  })

  it('puts the decision in a full-width sub-row, not the state column', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(changed() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    const consequence = await screen.findByText(
      /Approve to let one shared backend run this exact command/,
    )
    const cell = consequence.closest('td')
    expect(cell?.getAttribute('colspan')).toBe('4')
    // The pill stays where a column value belongs.
    expect(screen.getByText('command changed', { selector: 'span' }).closest('td')).not.toBe(cell)
  })
})
