// MCP Management: the top of the page.
//
// Contract under test:
// - the hero's numbers come from the footprint read, and the row is hidden
//   when either figure is missing
// - the banner shows only while sharing is on AND open chats still run their
//   own copies; its Reload asks first, then reloads each chat through the
//   dashboard's own per-chat reload, skipping (never forcing) a busy one
// - the approval card counts the servers that wait for an OK and can get it;
//   its button opens Advanced on those rows, one approval at a time
// - turning sharing on also enables shareable servers nobody chose for, never
//   one turned off by hand, a credential vendor, a non-eligible one or one
//   waiting for approval, and the confirm names exactly what it shares
//
// Every label is read from the catalog, so a wording change never breaks a test.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'
import { autoShareCandidates, reloadChats } from '../pages/settings/mcpManagementModel'

const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)

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

const status = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

const GIB = 1024 ** 3
const footprint = (over: Record<string, unknown> = {}) => ({
  using_now_bytes: 5.4 * GIB,
  with_sharing_bytes: 2.3 * GIB,
  open_chats: 3,
  stale_chats: [] as string[],
  ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** A launch block's text as drawn: the tokens are separate spans inside one <pre>. */
const pres = (root: HTMLElement) => Array.from(root.querySelectorAll('pre')).map(p => p.textContent)

/** Waits until the footprint read has answered (or failed), so an absence is real. */
const footprintSettled = () =>
  waitFor(
    () => expect(document.querySelector('[data-footprint]')?.getAttribute('data-footprint')).not.toBe('pending'),
    { timeout: 5000 },
  )

const findSharing = () => screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })

beforeEach(() => {
  vi.restoreAllMocks()
  // The hero's memory read: answered with no figures unless a test says otherwise.
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({ using_now_bytes: null, with_sharing_bytes: null, open_chats: 0, stale_chats: [] } as never)
  answerBatchPreviewsPerName()
})
afterEach(cleanup)

describe('the hero numbers', () => {
  it('shows what the footprint read measured, now and with sharing', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue(footprint() as never)

    mount()
    expect(await screen.findByText(M('gb_value', { value: '5.4' }), undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(M('gb_value_approx', { value: '2.3' }))).toBeTruthy()
    expect(screen.getByText(M('using_now'))).toBeTruthy()
    expect(screen.getByText(M('with_sharing'))).toBeTruthy()
  })

  it.each([
    ['the reading now', { using_now_bytes: null }],
    ['the reading with sharing', { with_sharing_bytes: null }],
  ])('hides the number row when %s is missing', async (_label, over) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const read = vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue(footprint(over) as never)

    mount()
    await findSharing()
    await waitFor(() => expect(read).toHaveBeenCalled(), { timeout: 5000 })
    await footprintSettled()
    expect(screen.queryByText(M('using_now'))).toBeNull()
    expect(screen.queryByText(M('with_sharing'))).toBeNull()
  })

  it('hides the number row and says so when the footprint request fails', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayFootprint').mockRejectedValue(new Error('down'))

    mount()
    await findSharing()
    await footprintSettled()
    expect(screen.queryByText(M('using_now'))).toBeNull()
    // A failed request is an error, through the shared notice, not a silent gap.
    expect(screen.getByRole('alert').textContent).toContain(M('footprint_failed'))
  })

  it('says the server list failed while Advanced is still closed', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockRejectedValue(new Error('down'))

    mount()
    // The failed list disables the switch; the reason must not hide in Advanced.
    expect(await screen.findByText(M('servers_failed'), undefined, { timeout: 8000 })).toBeTruthy()
    expect(screen.getByRole('button', { name: M('advanced') }).getAttribute('aria-expanded')).toBe('false')
  })
})

describe('the open-chats banner', () => {
  it('shows only while sharing is on and chats still run their own copies', async () => {
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const banner = M('stale_banner', { count: 2 })

    // On, two stale chats: shown, with the plural button.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue(footprint({ stale_chats: ['a', 'b'] }) as never)
    const first = mount()
    expect(await screen.findByText(banner, undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByRole('button', { name: M('reload_button', { count: 2 }) })).toBeTruthy()
    first.unmount()
    cleanup()

    // Off, same chats: hidden.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    const second = mount()
    await findSharing()
    await footprintSettled()
    expect(screen.queryByText(banner)).toBeNull()
    second.unmount()
    cleanup()

    // On, no stale chat: hidden.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue(footprint({ stale_chats: [] }) as never)
    mount()
    await findSharing()
    await footprintSettled()
    expect(screen.queryByText(M('stale_banner', { count: 0 }))).toBeNull()
    expect(screen.queryByRole('button', { name: M('reload_button', { count: 0 }) })).toBeNull()
  })

  it('asks first, reloads each chat, and says how many busy chats it skipped', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue(
      footprint({ stale_chats: ['idle-1', 'busy-1', 'idle-2'] }) as never,
    )
    const reload = vi.spyOn(api, 'chatSlotReload').mockImplementation((async (slot: string) => {
      if (slot.startsWith('busy')) throw new ApiError(409, 'turn in flight', '{"code":"turn_in_flight"}')
      return { ok: true }
    }) as never)

    mount()
    fireEvent.click(await screen.findByRole('button', { name: M('reload_button', { count: 3 }) }, { timeout: 5000 }))
    // Nothing reloads before the confirm.
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByText(M('reload_confirm_title', { count: 3 }))).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: M('reload_button', { count: 3 }) }))

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(3), { timeout: 5000 })
    expect(reload.mock.calls.map(c => c[0])).toEqual(['idle-1', 'busy-1', 'idle-2'])
    expect(await screen.findByText(M('reload_skipped', { count: 1 }), undefined, { timeout: 5000 })).toBeTruthy()
  })
})

describe('reloadChats', () => {
  it('skips a chat the gateway refuses as busy and never retries it', async () => {
    const reload = vi.fn(async (slot: string) => {
      if (slot === 'b') throw new ApiError(409, 'busy', '{"code":"turn_in_flight"}')
      if (slot === 'c') throw new ApiError(500, 'boom')
      return { ok: true }
    })
    const r = await reloadChats(['a', 'b', 'c', 'd'], reload)
    expect(r).toEqual({ done: ['a', 'd'], skipped: ['b'], failed: ['c'] })
    expect(reload).toHaveBeenCalledTimes(4)
  })
})

describe('the approval card', () => {
  const changed = { reason: 'changed_needs_reapproval', complete: true, expected_launch: 'x', commands: [['/a']] }
  const unapprovable = { reason: 'changed_needs_reapproval', complete: false }

  it('counts the servers that wait for an OK they can get, and names them', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp', 'beta-mcp', 'gamma-mcp'],
        launch_refused: { 'alpha-mcp': changed, 'beta-mcp': changed, 'gamma-mcp': unapprovable },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true }),
        server({ name: 'beta-mcp', stub: true, in_allowlist: true }),
        server({ name: 'gamma-mcp', stub: true, in_allowlist: true }),
      ],
    } as never)

    mount()
    const card = (await screen.findByText(M('approval_title', { count: 2 }), undefined, { timeout: 5000 })).closest(
      'section',
    ) as HTMLElement
    const chips = within(card).getAllByRole('listitem').map(li => li.textContent)
    expect(chips).toEqual(['alpha-mcp', 'beta-mcp'])
    // No approve-all: the only button opens the rows.
    expect(within(card).getAllByRole('button').map(b => b.textContent)).toEqual([M('review_approve')])
  })

  it('is not drawn when nothing waits', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    mount()
    await findSharing()
    expect(screen.queryByRole('button', { name: M('review_approve') })).toBeNull()
  })

  it('leaves Advanced collapsed by default', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    mount()
    const advanced = await screen.findByRole('button', { name: M('advanced') }, { timeout: 5000 })
    expect(advanced.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('table')).toBeNull()
  })
})

describe('the Review dialog', () => {
  const changedSearch = {
    reason: 'changed_needs_reapproval', complete: true, expected_launch: 'search-shown',
    commands: [['/opt/search', '--v2']], approved_commands: [['/opt/search', '--v1']],
    envs: [['TOKEN_X=<hidden sha256:00ff00ff>']], approved_envs: [['TOKEN_X=<hidden sha256:00ff00ff>']],
  }
  const newTickets = {
    reason: 'added_outside_dashboard', complete: true, expected_launch: 'tickets-shown', commands: [['/opt/tickets']],
  }

  function mockTwo() {
    let refused: Record<string, unknown> = { 'search-mcp': changedSearch, 'tickets-mcp': newTickets }
    vi.spyOn(api, 'mcpGatewayStatus').mockImplementation((async () =>
      status({ enabled: true, stub: ['search-mcp', 'tickets-mcp'], launch_refused: refused })) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'docs-mcp' }),
        server({ name: 'search-mcp', stub: true, in_allowlist: true }),
        server({ name: 'tickets-mcp', stub: true, in_allowlist: true }),
      ],
    } as never)
    return { approveAll: () => { refused = {} }, drop: (name: string) => { const { [name]: _gone, ...rest } = refused; void _gone; refused = rest } }
  }

  async function openDialog() {
    const button = await screen.findByRole('button', { name: M('review_approve') }, { timeout: 5000 })
    // A real click focuses the button; the dialog hands focus back to it.
    button.focus()
    fireEvent.click(button)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    return { button, dialog }
  }
  const block = (dialog: HTMLElement, name: string) =>
    within(dialog).getByRole('heading', { name }).closest('section') as HTMLElement

  it('lists only the waiting servers, with what each runs and env names only', async () => {
    mockTwo()
    mount()
    const { dialog } = await openDialog()
    expect(within(dialog).getByText(M('approval_title', { count: 2 }))).toBeTruthy()
    expect(within(dialog).getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['search-mcp', 'tickets-mcp'])
    const search = block(dialog, 'search-mcp')
    expect(within(search).getByText(M('command_changed_hint_command'))).toBeTruthy()
    expect(pres(search)).toEqual(['/opt/search --v1', '/opt/search --v2'])
    // What changed is said once, above the two columns, and marked in NEW.
    expect(within(search).getByText(M('diff_changed', { from: '--v1', to: '--v2' }))).toBeTruthy()
    expect(Array.from(search.querySelectorAll('mark')).map(m => m.textContent)).toEqual(['--v2'])
    expect(search.textContent).not.toContain('sha256')
    const tickets = block(dialog, 'tickets-mcp')
    expect(within(tickets).getByText(M('reapproval_reason_added_outside_dashboard_unapprovable'))).toBeTruthy()
    // Each block says what Approve and Skip do.
    expect(within(search).getByText(M('review_what_approve_does'))).toBeTruthy()
    // What approving risks and when it is fine is said once, above every block.
    const intro = `${M('share_risk')} ${M('review_when_safe')}`
    expect(Array.from(dialog.querySelectorAll('p')).filter(p => p.textContent === intro)).toHaveLength(1)
    // One Approve per server; no approve-all.
    expect(within(dialog).getAllByRole('button', { name: M('approve_button') })).toHaveLength(2)
  })

  it('approves with the fingerprint it showed, skip writes nothing, and it closes when all are handled', async () => {
    const ctl = mockTwo()
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => {
      ctl.drop(name)
      return { ok: true, name, stub: true }
    }) as never)
    mount()
    const { button, dialog } = await openDialog()
    fireEvent.click(within(block(dialog, 'search-mcp')).getByRole('button', { name: M('approve_button') }))
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('search-mcp', true, 'search-shown'), { timeout: 5000 })
    const outcome = await within(block(dialog, 'search-mcp')).findByRole('status', undefined, { timeout: 5000 })
    expect(outcome.textContent).toBe(M('review_approved'))
    // The status read is invalidated, so the card shrinks to what still waits,
    // and the dialog's own title counts only what still waits.
    await waitFor(() => expect(screen.getAllByText(M('approval_title', { count: 1 })).length).toBe(2), { timeout: 5000 })

    fireEvent.click(within(block(dialog, 'tickets-mcp')).getByRole('button', { name: M('review_skip') }))
    expect(setStub).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    await waitFor(() => expect(document.activeElement).toBe(button), { timeout: 5000 })
  })

  it('shows the refusal in the block when the launch changed underneath, and approves nothing else', async () => {
    mockTwo()
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(409, 'changed', '{"code":"launch_changed_since_display"}'),
    )
    mount()
    const { dialog } = await openDialog()
    const search = block(dialog, 'search-mcp')
    fireEvent.click(within(search).getByRole('button', { name: M('approve_button') }))
    expect(await within(search).findByRole('alert', undefined, { timeout: 5000 })).toBeTruthy()
    expect(within(search).getByRole('alert').textContent).toContain(M('stub_error_launch_changed', { name: 'search-mcp' }))
    expect(setStub).toHaveBeenCalledTimes(1)
    expect(setStub).toHaveBeenCalledWith('search-mcp', true, 'search-shown')
    // Still open and still pending: nothing was approved.
    expect(within(search).queryByRole('status')).toBeNull()
    expect(within(search).getByRole('button', { name: M('approve_button') })).toBeTruthy()
  })

  it('moves focus to the next pending Approve after an approve or a skip', async () => {
    const ctl = mockTwo()
    vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => {
      ctl.drop(name)
      return { ok: true, name, stub: true }
    }) as never)
    mount()
    const { dialog } = await openDialog()
    // Skip the first block: focus lands on the second block's Approve.
    fireEvent.click(within(block(dialog, 'search-mcp')).getByRole('button', { name: M('review_skip') }))
    const next = within(block(dialog, 'tickets-mcp')).getByRole('button', { name: M('approve_button') })
    await waitFor(() => expect(document.activeElement).toBe(next), { timeout: 5000 })
  })

  it('scrolls only its body, which keeps overscroll off the page', async () => {
    mockTwo()
    mount()
    const { dialog } = await openDialog()
    const body = within(dialog).getByTestId('mcp-review-body')
    expect(body.className).toContain('overflow-y-auto')
    expect(body.className).toContain('overscroll-contain')
    // Long commands wrap inside their block instead of widening the dialog.
    for (const pre of Array.from(dialog.querySelectorAll('pre'))) {
      expect(pre.className).toContain('whitespace-pre-wrap')
      expect(pre.className).toContain('[overflow-wrap:anywhere]')
      // Never mid-token at any width: a version number must not split.
      expect(pre.className).not.toContain('break-all')
    }
  })

  it('closes on Escape and returns focus to the button', async () => {
    mockTwo()
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')
    mount()
    const { button, dialog } = await openDialog()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    await waitFor(() => expect(document.activeElement).toBe(button), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('hides the approval card once the last waiting server is approved', async () => {
    const ctl = mockTwo()
    const spy = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => {
      ctl.drop(name)
      return { ok: true, name, stub: true }
    }) as never)
    const setStubCalls = () => spy.mock.calls.map(c => c[0])
    mount()
    const { dialog } = await openDialog()
    for (const name of ['search-mcp', 'tickets-mcp']) {
      fireEvent.click(within(block(dialog, name)).getByRole('button', { name: M('approve_button') }))
      await waitFor(() => expect(setStubCalls()).toContain(name), { timeout: 5000 })
    }
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    await waitFor(() => expect(screen.queryByRole('button', { name: M('review_approve') })).toBeNull(), { timeout: 5000 })
    void ctl
  })
})

describe('turning sharing on', () => {
  const changed = { reason: 'changed_needs_reapproval', complete: true, expected_launch: 'x' }
  const rows = [
    server({ name: 'never-set' }),
    server({ name: 'turned-off', declined: true }),
    server({ name: 'already-on', stub: true, in_allowlist: true }),
    server({ name: 'creds-agent', can_stub: false, denylisted: true }),
    server({ name: 'http-one', can_stub: false, transport: 'http' }),
    server({ name: 'env-blocked', pooling_blocked_by_env: true }),
    server({ name: 'waiting', stub: true, in_allowlist: true }),
  ]

  it('picks only shareable servers nobody chose for', () => {
    const picked = autoShareCandidates(rows as never, { waiting: changed } as never).map(s => s.name)
    expect(picked).toEqual(['never-set'])
  })

  it('names exactly what it shares in the confirm, then turns sharing on before the pinned writes', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: false, stub: ['already-on', 'waiting'], launch_refused: { waiting: changed } }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: rows } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation((async (name: string) => ({
      name, commands: [['/x']], envs: [[]], complete: true, expected_launch: `${name}-id`,
    })) as never)
    const order: string[] = []
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockImplementation((async () => {
      order.push('enable')
      return { ping_ok: true }
    }) as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => {
      order.push(`stub:${name}`)
      return { ok: true, name, stub: true, stubbed: [name] }
    }) as never)

    mount()
    const sharing = await findSharing()
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    // Read only for the one server it would enable.
    expect(preview.mock.calls.map(c => c[0])).toEqual(['never-set'])
    // The list: the one marked by hand, then the one it enables. Nothing else.
    expect(dialog.textContent).toContain('already-on, never-set')
    for (const name of ['turned-off', 'creds-agent', 'http-one', 'env-blocked', 'waiting']) {
      expect(dialog.textContent).not.toContain(name)
    }
    expect(enable).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: M('confirm_turn_on') }))

    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    expect(order).toEqual(['enable', 'stub:never-set'])
    expect(setStub).toHaveBeenCalledWith('never-set', true, 'never-set-id', true)
  })

  it('turning it off writes no per-server choice, so the next on restores them', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['already-on'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ name: 'already-on', stub: true, in_allowlist: true }), server({ name: 'turned-off', declined: true })],
    } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const sharing = await findSharing()
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    await waitFor(() => expect(enable).toHaveBeenCalledWith(false), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
    // And a server turned off by hand is never a candidate for the next on.
    expect(autoShareCandidates([server({ name: 'turned-off', declined: true })] as never).length).toBe(0)
  })

  it('shows a failed launch read as an error, not a skip, and shares nothing for it', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ name: 'never-set' })] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(new ApiError(503, 'down', '{"code":"launch_resolve_failed"}'))
    vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const sharing = await findSharing()
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByRole('alert').textContent).toContain(
      M('enable_all_review_failed_names', { names: 'never-set' }),
    )
    expect(within(dialog).queryByText(M('enable_all_review_skipped_names', { names: 'never-set' }))).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: M('confirm_turn_on') }))
    await waitFor(() => expect(api.mcpGatewayEnable).toHaveBeenCalledWith(true), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('names a server whose launch cannot be approved as skipped, and shares nothing for it', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ name: 'never-set' })] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue({
      name: 'never-set', commands: [], envs: [], complete: false,
    } as never)
    vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const sharing = await findSharing()
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByText(M('enable_all_review_skipped_names', { names: 'never-set' }))).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: M('confirm_turn_on') }))
    await waitFor(() => expect(api.mcpGatewayEnable).toHaveBeenCalledWith(true), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })
})
