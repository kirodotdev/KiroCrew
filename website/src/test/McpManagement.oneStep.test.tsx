// MCP Management: one switch shares a server.
//
// Contract under test:
// - each row's control is one <button role="switch"> that carries its own
//   words, never a bare track; "Approve update" is a real button
// - sharing is ONE request: no review dialog (the endpoint records the exact
//   launch itself)
// - turning a row on never turns page-wide sharing on; that switch asks first
// - a shared row whose command changed reads "Paused · command changed", and its
//   approve button pins the refused launch's own identity
// - a row shows no launch command until its Details opens, and then only the
//   gateway's display-safe form (argv, env KEY names, never a value); one press
//   reads the launch and writes the share pinned to it; Enable all lists
//   servers by name and shares only on a second press, pinned to the launch
//   read for each
//
// Every label is read from the catalog, so a wording change never breaks a test.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'

/** The page's own English copy. */
const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)

/** A row control's accessible name: its visible label, then the row's server name. */
const rowName = (labelKey: string, server: string) => `${M(labelKey)} ${server}`

/** A row's share switch, bounded so a hang fails here. */
const findRowSwitch = (server = 'alpha-mcp') =>
  screen.findByRole('switch', { name: rowName('share_toggle', server) }, { timeout: 5000 })

/** A launch block's text as drawn: the tokens are separate spans inside one <pre>. */
const pres = (root: HTMLElement) => Array.from(root.querySelectorAll('pre')).map(p => p.textContent)

/** Opens a row's Details and returns its sub-row. */
async function openDetails(name = 'alpha-mcp') {
  const toggle = await screen.findByRole('button', { name: M('details_aria', { name }) }, { timeout: 5000 })
  fireEvent.click(toggle)
  const id = toggle.getAttribute('aria-controls') as string
  await waitFor(() => expect(document.getElementById(id)).not.toBeNull(), { timeout: 5000 })
  return document.getElementById(id) as HTMLElement
}

function server(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['kirocrew'],
    transport: 'stdio',
    denylisted: false,
    ...over,
  }
}

function launch(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    commands: [['/opt/alpha', '--one']],
    envs: [[]],
    complete: true,
    expected_launch: 'alpha-identity',
    ...over,
  }
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement initialAdvancedOpen />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  // The hero's memory read: answered with no figures unless a test says otherwise.
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({ using_now_bytes: null, with_sharing_bytes: null, open_chats: 0, stale_chats: [] } as never)
  answerBatchPreviewsPerName()
})
afterEach(cleanup)

describe('one-step sharing', () => {
  it('shares on one press, with no review dialog', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true, name: 'alpha-mcp', stub: true, restart_required: true,
    } as never)

    const { container } = mount()
    const share = await findRowSwitch()
    expect(share.tagName).toBe('BUTTON')
    expect(share.getAttribute('aria-checked')).toBe('false')
    // The words are inside the control, so the whole label is the target.
    expect(share.textContent).toContain(M('share_toggle'))
    // Nothing is read before the press, and no command is on the page.
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()
    expect((share as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(share)

    // Chain: the press -> the launch read -> one stub POST pinned to it.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    expect(api.mcpGatewayLaunchPreview).toHaveBeenCalledWith('alpha-mcp')
    // The launch approved is exactly the one read for this press.
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity')
    expect(container.textContent).not.toContain('/opt/alpha')
    // Nothing asks a second time.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps the confirm closed when the marked list cannot be read back', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers')
      .mockResolvedValueOnce({ servers: [server()] } as never)
      .mockRejectedValue(new Error('read failed'))
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await findRowSwitch())
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    // Chain: the stub POST -> the read-back fails -> no dialog.
    expect(await screen.findByText(M('servers_failed'), undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('marks the server, then asks before turning page-wide sharing on', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    fireEvent.click(await findRowSwitch())
    // Chain: the press -> the launch read -> the stub POST -> the switch's own confirm.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(enable).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: M('confirm_turn_on') }))
    await waitFor(() => expect(enable).toHaveBeenCalledWith(true), { timeout: 5000 })
  })

  it('stops sharing when its switch is turned off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const stop = await findRowSwitch()
    expect(stop.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(stop)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined), { timeout: 5000 })
  })

  it('approves a changed command pinned to the refused launch, its before and after in Details', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['/opt/alpha', '--two']],
            envs: [['MODE=new']],
            approved_commands: [['/opt/alpha', '--one']],
            approved_envs: [['MODE=old']],
            complete: true,
            expected_launch: 'pinned-identity',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    const { container } = mount()
    const approve = await screen.findByRole('button', { name: rowName('new_command_button', 'alpha-mcp') }, { timeout: 5000 })
    expect(approve.textContent).toContain(M('new_command_button'))
    expect(screen.getAllByText(M('state_needs_reapproval')).length).toBeGreaterThan(0)
    // The row keeps the chip and the button; the "why" waits in Details.
    expect(screen.queryByText(M('command_changed_hint_command'))).toBeNull()
    // Nothing runs shared while it is paused, so the switch beside it reads off.
    expect((await findRowSwitch()).getAttribute('aria-checked')).toBe('false')
    expect(container.textContent).not.toContain('/opt/alpha')

    const details = await openDetails()
    expect(within(details).getByText(M('command_changed_hint_command'))).toBeTruthy()
    expect(within(details).getByText(M('details_approved'))).toBeTruthy()
    expect(within(details).getByText(M('details_new'))).toBeTruthy()
    expect(pres(details)).toEqual(['/opt/alpha --one', '/opt/alpha --two'])
    // Env KEY names only: no value, not even the old one.
    expect(within(details).getAllByText('MODE').length).toBe(2)
    expect(details.textContent).not.toContain('MODE=')
    // The Approve button now points at the line that says why.
    expect(approve.getAttribute('aria-describedby')).toBe(`mcp-row-why-alpha-mcp`)
    fireEvent.click(approve)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'pinned-identity'), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledTimes(1)
    // The refusal carries the identity; nothing is read again to approve it.
    expect(preview).not.toHaveBeenCalled()
  })

  it('names a server never approved here as such, not as a changed command', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': { reason: 'added_outside_dashboard', complete: true, expected_launch: 'first-identity' },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const approve = await screen.findByRole('button', { name: rowName('approve_button', 'alpha-mcp') }, { timeout: 5000 })
    expect(screen.getAllByText(M('state_not_approved_yet')).length).toBeGreaterThan(0)
    expect(screen.queryByText(M('reapproval_reason_added_outside_dashboard_unapprovable'))).toBeNull()
    const details = await openDetails()
    expect(within(details).getByText(M('reapproval_reason_added_outside_dashboard_unapprovable'))).toBeTruthy()
    // No before to compare: one Runs block, no Approved/New pair.
    expect(within(details).getByText(M('details_runs'))).toBeTruthy()
    expect(within(details).queryByText(M('details_approved'))).toBeNull()
    expect(screen.queryByText(M('state_needs_reapproval'))).toBeNull()
    expect(screen.queryByRole('button', { name: rowName('new_command_button', 'alpha-mcp') })).toBeNull()
    // Never approved, so nothing runs shared: the switch reads off.
    expect((await findRowSwitch()).getAttribute('aria-checked')).toBe('false')
    fireEvent.click(approve)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'first-identity'), { timeout: 5000 })
  })

  it.each([
    ['never approved here', { reason: 'added_outside_dashboard', complete: true, expected_launch: 'first-identity' }, 'approve_button'],
    [
      'command changed',
      {
        reason: 'changed_needs_reapproval', complete: true, expected_launch: 'changed-identity',
        commands: [['/opt/alpha', '--two']], approved_commands: [['/opt/alpha', '--one']],
      },
      'new_command_button',
    ],
  ])('sends the off switch of a refused row (%s) to its Approve button, writing nothing', async (_l, refusal, buttonKey) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], launch_refused: { 'alpha-mcp': refusal } }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview')

    mount()
    const control = await findRowSwitch()
    expect(control.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(control)

    // The switch is not an approval: only the button beside it approves.
    const approve = screen.getByRole('button', { name: rowName(buttonKey, 'alpha-mcp') })
    expect(document.activeElement).toBe(approve)
    // The press is synchronous: it only moves focus, so nothing can be in flight.
    expect(setStub).not.toHaveBeenCalled()
    expect(preview).not.toHaveBeenCalled()
  })

  it('counts every row it lists in the header, a blocked one too', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true }),
        server({ name: 'beta-mcp' }),
        server({ name: 'gamma-mcp', can_stub: false }),
      ],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText(M('stubbed_of_total', { stubbed: 1, total: 3 }), undefined, { timeout: 5000 })).toBeTruthy()
  })

  it('reads a marked row as waiting, and does not count it, while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText(M('stubbed_of_total', { stubbed: 0, total: 2 }), undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getAllByText(M('state_stub')).length).toBeGreaterThan(0)
  })

  it('offers no approve for a refused launch with no approvable identity, and keeps a way to turn it off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: { 'alpha-mcp': { reason: 'changed_needs_reapproval', complete: false } },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    expect(await screen.findByText(M('approval_not_approvable_here'), undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByRole('button', { name: rowName('new_command_button', 'alpha-mcp') })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
    // Its chip says it cannot be shared, not that a fixable change is waiting.
    expect(screen.getAllByText(M('state_no_stub')).length).toBeGreaterThan(0)
    expect(screen.queryByText(M('state_needs_reapproval'))).toBeNull()
    // A marked row that cannot share has no switch (an "on" switch beside
    // "Can't share" gives two answers); one Remove button takes the mark off.
    expect(screen.queryByRole('switch', { name: rowName('share_toggle', 'alpha-mcp') })).toBeNull()
    const remove = screen.getByRole('button', { name: rowName('remove_mark', 'alpha-mcp') })
    fireEvent.click(remove)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalledWith('alpha-mcp', true, expect.anything())
  })

  it('lays the servers out in the SERVER | USED BY | STATE | SHARE columns, stacking below md', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const control = await findRowSwitch()
    expect(screen.getAllByRole('columnheader').map(h => h.textContent)).toEqual([
      M('col_server'),
      M('col_used_by'),
      M('col_state'),
      M('col_share'),
    ])
    // jsdom has no layout, so this pins the rule: the table is a block of
    // stacked rows until md, where it becomes a real table.
    const table = control.closest('table') as HTMLElement
    expect(table.className.split(' ')).toEqual(expect.arrayContaining(['block', 'md:table']))
    const row = control.closest('tr') as HTMLElement
    expect(row.className.split(' ')).toContain('md:table-row')
  })

  it('never says "stub" anywhere a person reads on the page', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)

    const { container } = mount()
    await findRowSwitch()
    expect(container.textContent?.toLowerCase()).not.toContain('stub')
  })
})

describe('one vocabulary', () => {
  it('words the page-wide confirm without the retired "marked"', () => {
    for (const count of [1, 3]) {
      const lede = M('confirm_will_share', { count }).toLowerCase()
      expect(lede).not.toContain('marked')
      expect(lede).not.toContain('stub')
    }
  })
})

describe('accessible names', () => {
  it.each([true, false])('start with the visible label, which is the same either way (sharing on: %s)', async on => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: on }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const control = await findRowSwitch()
    expect(control.textContent).toContain(M('share_toggle'))
  })
})

describe('the page-wide switch', () => {
  it('keeps the switch turnable off when sharing is on with nothing marked', async () => {
    // Reachable by turning off the last server: the switch must stay escapable.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ can_stub: false })] } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    expect(sharing.getAttribute('aria-checked')).toBe('true')
  })

  it('lists only the servers it will share, and names a warned one without a second write control', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: false,
        stub: ['alpha-mcp', 'beta-mcp'],
        launch_refused: { 'beta-mcp': { reason: 'added_outside_dashboard', complete: true, expected_launch: 'b' } },
      }) as never,
    )
    const refuted = { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true, recommendation: refuted }),
        server({ name: 'beta-mcp', stub: true, in_allowlist: true }),
      ],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(dialog.textContent).toContain(M('confirm_will_share', { count: 1 }))
    expect(dialog.textContent).not.toContain('beta-mcp')
    // The warned server is named; leaving it out is its own row's switch, so
    // the confirm carries no second control that writes.
    expect(within(dialog).getByText(M('confirm_unsupported'))).toBeTruthy()
    expect(within(dialog).getAllByRole('button').map(b => b.textContent)).not.toContain('alpha-mcp')
    expect(setStub).not.toHaveBeenCalled()
  })

  it('offers the same approve button for a changed row while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: false,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            complete: true,
            expected_launch: 'x',
            commands: [['/opt/a', '--two']],
            approved_commands: [['/opt/a', '--one']],
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)

    mount()
    expect(await screen.findByRole('button', { name: rowName('new_command_button', 'alpha-mcp') }, { timeout: 5000 })).toBeTruthy()
    const details = await openDetails()
    expect(within(details).getByText(M('command_changed_hint_command'))).toBeTruthy()
  })

  it.each([
    [{ commands: [['/opt/a', '--two']], approved_commands: [['/opt/a', '--one']] }, 'command_changed_hint_command'],
    [
      { commands: [['/opt/a']], approved_commands: [['/opt/a']], envs: [['MODE=new']], approved_envs: [['MODE=old']] },
      'command_changed_hint_settings',
    ],
  ])('says in Details what kind of change paused a server, with no env value', async (change, key) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': { reason: 'changed_needs_reapproval', complete: true, expected_launch: 'x', ...change },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)

    const { container } = mount()
    // The row itself shows neither the reason nor the command.
    await screen.findByRole('button', { name: rowName('new_command_button', 'alpha-mcp') }, { timeout: 5000 })
    expect(screen.queryByText(M(key))).toBeNull()
    expect(container.textContent).not.toContain('/opt/a')
    const details = await openDetails()
    expect(within(details).getByText(M(key))).toBeTruthy()
    expect(container.textContent).not.toContain('MODE=')
  })

  it('names the marked servers in its confirm', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByText('alpha-mcp')).toBeTruthy()
    expect(within(dialog).queryByText('beta-mcp')).toBeNull()
  })

  it('offers one Remove button, not an on switch, on a marked row its env keeps unshared', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true, pooling_blocked_by_env: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const remove = await screen.findByRole('button', { name: rowName('remove_mark', 'alpha-mcp') }, { timeout: 5000 })
    expect(screen.queryByRole('switch', { name: rowName('share_toggle', 'alpha-mcp') })).toBeNull()
    fireEvent.click(remove)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined), { timeout: 5000 })
  })

  it.each([
    ['sharing is off', false, {}],
  ])('keeps a marked row on and turnable off when %s', async (_why, enabled, over) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true, ...over })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const control = await findRowSwitch()
    expect(control.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(control)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined), { timeout: 5000 })
  })

  it('asks before it turns sharing on, and turns it on only on the confirm', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(enable).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: M('confirm_turn_on') }))

    await waitFor(() => expect(enable).toHaveBeenCalledWith(true), { timeout: 5000 })
  })

  it('names, in the confirm, marked servers the check argues against', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['beta-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'beta-mcp', stub: true, in_allowlist: true, recommendation: { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] } }),
      ],
    } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    // Named at the decision point, before the press that turns sharing on.
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByText(M('confirm_unsupported'))).toBeTruthy()
    expect(within(dialog).getAllByText('beta-mcp').length).toBeGreaterThan(0)
  })
})

describe('the launch a share approves', () => {
  it('turns nothing on when the launch read at the press cannot be approved', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch({ complete: false, expected_launch: undefined }) as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await findRowSwitch())
    // Chain: the press -> the launch read -> the refusal line; bounded so a hang fails here.
    expect(await screen.findByText(M('approval_not_approvable_here'), undefined, { timeout: 5000 })).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('says nothing was turned on when the launch read fails', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(new Error('503'))
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await findRowSwitch())
    expect(
      await screen.findByText(M('launch_preview_error_failed', { name: 'alpha-mcp' }), undefined, { timeout: 5000 }),
    ).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reads the launch afresh on every press, so a press after a refusal pins the current one', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const preview = vi
      .spyOn(api, 'mcpGatewayLaunchPreview')
      .mockResolvedValueOnce(launch() as never)
      .mockResolvedValue(launch({ commands: [['/opt/alpha', '--moved']], expected_launch: 'moved-identity' }) as never)
    const setStub = vi
      .spyOn(api, 'mcpGatewaySetStub')
      .mockRejectedValueOnce(new Error('launch_changed_since_display'))
      .mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    const { container } = mount()
    fireEvent.click(await findRowSwitch())
    // Chain: the press -> launch read -> refused POST.
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity'), { timeout: 5000 })
    const share = await findRowSwitch()
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(share)
    await waitFor(() => expect(setStub).toHaveBeenLastCalledWith('alpha-mcp', true, 'moved-identity'), { timeout: 5000 })
    expect(preview).toHaveBeenCalledTimes(2)
    expect(container.textContent).not.toContain('/opt/alpha')
  })

  it('reads what a server runs only when its Details open, showing env key names and no value', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      launch({
        commands: [['/opt/alpha', '--root', '/my dir']],
        envs: [['API_TOKEN=<hidden sha256:0123abcd>', 'MODE=${MODE}']],
      }) as never,
    )

    const { container } = mount()
    await findRowSwitch()
    const toggle = screen.getByRole('button', { name: M('details_aria', { name: 'alpha-mcp' }) })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(preview).not.toHaveBeenCalled()

    const details = await openDetails()
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // A full-width sub-row under the table row.
    expect(details.tagName).toBe('TR')
    expect(details.querySelector('td')?.getAttribute('colspan')).toBe('4')
    expect(within(details).getByText(M('why_shared'))).toBeTruthy()
    await waitFor(() => expect(pres(details)).toEqual(['/opt/alpha --root "/my dir"']), { timeout: 5000 })
    expect(preview).toHaveBeenCalledWith('alpha-mcp')
    expect(details.textContent).toContain('API_TOKEN, MODE')
    // No value, not even the gateway's redacted form, and no repeat of USED BY.
    expect(details.textContent).not.toContain('=')
    expect(details.textContent).not.toContain('sha256')
    expect(details.textContent).not.toContain('kirocrew')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('/opt/alpha')
  })

  it.each([
    ['a direct server', server(), 'why_direct'],
    ['a server marked while sharing is off', server({ stub: true, in_allowlist: true }), 'why_stub_off'],
    ['a credential vendor', server({ name: 'creds-agent', can_stub: false, denylisted: true }), 'why_credentials'],
    ['a server with no stdio pipe', server({ can_stub: false, transport: 'http' }), 'why_no_stdio'],
    [
      'a server whose env keeps it per chat',
      server({ stub: true, in_allowlist: true, pooling_blocked_by_env: true }),
      'state_direct_env_reason',
    ],
    [
      'a server on a backend of its own while sharing is on',
      server({ stub: true, in_allowlist: true, private_backend: true }),
      'why_private_backend',
    ],
  ])('says in Details why %s is in its state', async (_label, row, key) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: key !== 'why_stub_off' }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [row] } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch({ name: row.name }) as never)

    mount()
    const details = await openDetails(row.name as string)
    expect(within(details).getByText(M(key))).toBeTruthy()
    if (row.transport === 'http') {
      // Nothing to run, so nothing is read.
      expect(preview).not.toHaveBeenCalled()
    } else {
      await waitFor(() => expect(pres(details)).toEqual(['/opt/alpha --one']), { timeout: 5000 })
    }
  })

  it('says it could not read the launch when the read fails, and turns nothing on', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(new Error('boom'))
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const details = await openDetails()
    expect(await within(details).findByText(M('details_runs_failed'), undefined, { timeout: 5000 })).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })
})

describe('enable all shareable', () => {
  function mountTwo(enabled = true) {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled }) as never)
    const declared = {
      strength: 'declared',
      recommendStub: true,
      recommendShare: true,
      reasons: [{ code: 'preflight_passed', detail: '' }],
    }
    const servers = {
      servers: [
        { ...server(), recommendation: declared },
        { ...server({ name: 'beta-mcp' }), recommendation: declared },
      ],
    }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue(servers as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation(
      async (name: string) =>
        launch({
          name,
          commands: [[`/opt/${name}`, '--serve']],
          expected_launch: `${name}-identity`,
        }) as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation(
      async (name: string) => ({ ok: true, name, stub: true, stubbed: [name] }) as never,
    )
    mount()
    return setStub
  }

  /** Press the page-wide action once it can be pressed. */
  async function pressEnableAll() {
    const all = await screen.findByRole('button', { name: M('enable_all') }, { timeout: 5000 })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)
  }

  const findList = () => screen.findByRole('region', { name: M('share_all_list_aria') }, { timeout: 5000 })

  it('lists every server by name first, with no command, and shares nothing on the first press', async () => {
    const setStub = mountTwo()
    await pressEnableAll()

    // Chain: servers re-read -> one batched launch read -> the list renders.
    const list = await findList()
    expect(list.textContent).toContain('alpha-mcp')
    expect(list.textContent).toContain('beta-mcp')
    expect(list.textContent).not.toContain('/opt/')
    expect(setStub).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: M('share_all_cancel') }))
    await waitFor(() => expect(screen.queryByRole('region', { name: M('share_all_list_aria') })).toBeNull(), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reports a failed launch read as an error, not as nothing to share', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(async (name: string) => {
      if (name === 'beta-mcp') throw new Error('read failed')
      return launch({ name, commands: [[`/opt/${name}`, '--serve']], expected_launch: `${name}-identity` }) as never
    })
    await pressEnableAll()

    // Chain: servers re-read -> one batched launch read -> error + list.
    expect(
      await screen.findByText(M('enable_all_review_failed_names', { names: 'beta-mcp' }), { exact: false }, { timeout: 5000 }),
    ).toBeTruthy()
    const list = await findList()
    expect(list.textContent).not.toContain('beta-mcp')
    expect(setStub).not.toHaveBeenCalled()
  })

  it('shares exactly the launches it read for the servers it listed', async () => {
    const setStub = mountTwo()
    await pressEnableAll()

    // Chain: servers re-read -> one batched launch read -> the confirm button.
    const confirm = await screen.findByRole('button', { name: M('share_all_confirm', { count: 2 }) }, { timeout: 5000 })
    expect(confirm.tagName).toBe('BUTTON')
    fireEvent.click(confirm)

    // Chain: one stub POST per listed server, sent in turn.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-mcp-identity', true)
    expect(setStub).toHaveBeenCalledWith('beta-mcp', true, 'beta-mcp-identity', true)
  })

  it('reports shared and asks nothing when the writes say sharing is already on', async () => {
    const setStub = mountTwo(false)
    setStub.mockImplementation(async (name: string) => ({ ok: true, name, stub: true, stubbed: [name], sharing_on: true }) as never)
    vi.mocked(api.mcpGatewayStatus).mockResolvedValue(status({ enabled: false }) as never)
    await pressEnableAll()
    // Another tab turns sharing on before this one confirms.
    vi.mocked(api.mcpGatewayStatus).mockResolvedValue(status({ enabled: true }) as never)
    fireEvent.click(await screen.findByRole('button', { name: M('share_all_confirm', { count: 2 }) }, { timeout: 5000 }))

    expect(
      await screen.findByText(M('enable_all_review_result_shared', { count: 2 }), { exact: false }, { timeout: 5000 }),
    ).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says it enabled, not shared, the listed servers while sharing is off, then asks', async () => {
    const setStub = mountTwo(false)
    const enable = vi.spyOn(api, 'mcpGatewayEnable')
    await pressEnableAll()
    fireEvent.click(await screen.findByRole('button', { name: M('share_all_confirm', { count: 2 }) }, { timeout: 5000 }))

    // Chain: one stub POST per listed server -> the result says enabled -> the switch's confirm.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(
      await screen.findByText(M('enable_all_review_result_marked', { count: 2 }), { exact: false }, { timeout: 5000 }),
    ).toBeTruthy()
    expect(screen.queryByText(M('enable_all_review_result_shared', { count: 2 }), { exact: false })).toBeNull()
    expect(await screen.findByRole('dialog', undefined, { timeout: 5000 })).toBeTruthy()
    expect(enable).not.toHaveBeenCalled()
  })

  it('puts the rows it re-read into the shared cache, so no view keeps a stale verdict', async () => {
    const declared = {
      strength: 'declared',
      recommendStub: true,
      recommendShare: true,
      reasons: [{ code: 'preflight_passed', detail: '' }],
    }
    const first = { servers: [{ ...server(), recommendation: declared }] }
    const fresh = {
      servers: [
        { ...server(), recommendation: declared },
        { ...server({ name: 'gamma-mcp' }), recommendation: declared },
      ],
    }
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValueOnce(first as never).mockResolvedValue(fresh as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation(
      async (name: string) => launch({ name, expected_launch: `${name}-identity` }) as never,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpManagement initialAdvancedOpen />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await pressEnableAll()

    // Chain: the fresh servers read -> the shared cache holds exactly that response.
    await findList()
    expect(qc.getQueryData(['mcpGatewayServers'])).toEqual(fresh)
  })

  it('lets a long server name wrap in the list instead of being clipped', async () => {
    mountTwo()
    await pressEnableAll()

    // Chain: servers re-read -> one batched launch read -> the list renders.
    // jsdom has no layout, so this pins the wrap rule the card's overflow-hidden needs.
    const list = await findList()
    expect(list.className).toContain('[overflow-wrap:anywhere]')
    expect(list.className).toContain('min-w-0')
  })

  it('names a candidate it leaves out inside the list', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(async (name: string) =>
      (name === 'beta-mcp'
        ? launch({ name, complete: false, expected_launch: undefined })
        : launch({ name, expected_launch: `${name}-identity` })) as never,
    )
    await pressEnableAll()

    const list = await findList()
    expect(list.textContent).toContain(M('enable_all_review_skipped_names', { names: 'beta-mcp' }))
    expect(setStub).not.toHaveBeenCalled()
  })

  it('names the skipped servers, not "nothing left", when none of them can be approved', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(
      async (name: string) => launch({ name, complete: false, expected_launch: undefined }) as never,
    )
    await pressEnableAll()

    // Chain: servers re-read -> every launch read is unapprovable -> the skipped names, no list.
    expect(
      await screen.findByText(M('enable_all_review_skipped_names', { names: 'alpha-mcp, beta-mcp' }), undefined, { timeout: 5000 }),
    ).toBeTruthy()
    expect(screen.queryByText(M('enable_all_review_nothing'))).toBeNull()
    expect(screen.queryByRole('region', { name: M('share_all_list_aria') })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
  })
})
