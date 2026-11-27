import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, type McpManagedServer } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { HERO_SWITCH_ID, ProjectShareSwitch } from '../pages/settings/McpManagementLaunchReview'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'

const M = (key: string) => i18nT(`pages.mcpManagement.${key}`)

function server(over: Partial<McpManagedServer> = {}): McpManagedServer {
  return {
    name: 'files',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['a'],
    transport: 'stdio',
    denylisted: false,
    per_project: true,
    ...over,
  } as McpManagedServer
}

describe('the per-project Share switch', () => {
  it('is on by default, and turning it off keeps the server one per chat', () => {
    const onChange = vi.fn()
    render(<ProjectShareSwitch server={server()} busy={false} onChange={onChange} />)
    const toggle = screen.getByRole('switch')
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('reads off and is disabled while page-wide sharing is off, described by the page-wide switch', () => {
    render(<ProjectShareSwitch server={server()} busy={false} sharingOn={false} onChange={vi.fn()} />)
    const toggle = screen.getByRole('switch')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect((toggle as HTMLButtonElement).disabled).toBe(true)
    expect(toggle.getAttribute('aria-describedby')).toBe(HERO_SWITCH_ID)
  })

  it('is off under the stored choice, and turning it on drops that choice', () => {
    const onChange = vi.fn()
    render(<ProjectShareSwitch server={server({ declined: true })} busy={false} onChange={onChange} />)
    const toggle = screen.getByRole('switch')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    expect(onChange).toHaveBeenCalledWith(false)
  })
})

describe('a per-project row whose declared env keeps it per chat', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({
      using_now_bytes: null,
      with_sharing_bytes: null,
      open_chats: 0,
      stale_chats: [],
    } as never)
    answerBatchPreviewsPerName()
  })
  afterEach(cleanup)

  it('draws no switch and says why on the row', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true,
      stub: [],
      stub_count: 0,
      running: true,
      ping_ok: true,
      supported: true,
    } as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ pooling_blocked_by_env: true }), server({ name: 'git' })],
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpManagement />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    // The other per-project row still has its switch, so the page has loaded.
    await screen.findByRole('switch', { name: `${M('share_toggle')} git` }, { timeout: 5000 })
    expect(screen.queryByRole('switch', { name: `${M('share_toggle')} files` })).toBeNull()
    expect(await screen.findByText(M('state_direct_env_reason'))).toBeTruthy()
  })

  it.each([
    ['vault', 'why_private_backend'],
    ['unreadable', 'why_declarations_unreadable'],
  ] as const)('a %s server the gateway runs one per chat offers no Share press', async (reason, why) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true,
      stub: [],
      stub_count: 0,
      running: true,
      ping_ok: true,
      supported: true,
    } as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'vault-tool', per_project: false, per_chat: true, per_chat_reason: reason }),
        server({ name: 'git' }),
      ],
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpManagement />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await screen.findByRole('switch', { name: `${M('share_toggle')} git` }, { timeout: 5000 })
    expect(screen.queryByRole('switch', { name: `${M('share_toggle')} vault-tool` })).toBeNull()
    expect(await screen.findByText(M(why))).toBeTruthy()
  })
})

describe('the row switches while page-wide sharing is off', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({
      using_now_bytes: null,
      with_sharing_bytes: null,
      open_chats: 0,
      stale_chats: [],
    } as never)
    answerBatchPreviewsPerName()
  })
  afterEach(cleanup)

  const status = (enabled: boolean) => ({
    enabled,
    stub: enabled ? ['notes'] : [],
    stub_count: 1,
    running: true,
    ping_ok: true,
    supported: true,
  })

  function renderPage(servers: McpManagedServer[]) {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status(false) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpManagement />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    return qc
  }

  const rowSwitch = (name: string) => screen.getByRole('switch', { name: `${M('share_toggle')} ${name}` })

  it('all read off and disabled, and the saved choices come back when it is turned on', async () => {
    const qc = renderPage([
      server({ name: 'git' }),
      server({ name: 'files', declined: true }),
      server({ name: 'notes', per_project: false, stub: true, in_allowlist: true }),
    ])
    const hero = await screen.findByRole('switch', { name: M('share_switch_label') }, { timeout: 5000 })
    await screen.findByRole('switch', { name: `${M('share_toggle')} git` })
    for (const name of ['git', 'files', 'notes']) {
      const toggle = rowSwitch(name)
      expect(toggle.getAttribute('aria-checked')).toBe('false')
      expect((toggle as HTMLButtonElement).disabled).toBe(true)
      expect(toggle.getAttribute('aria-describedby')?.split(' ')).toContain(hero.id)
    }
    expect((hero as HTMLButtonElement).disabled).toBe(false)

    // Sharing on: each row shows what it had saved, the declined one included.
    act(() => qc.setQueryData(['mcpGatewayStatus'], status(true)))
    await waitFor(() => expect(rowSwitch('git').getAttribute('aria-checked')).toBe('true'))
    expect(rowSwitch('files').getAttribute('aria-checked')).toBe('false')
    expect(rowSwitch('notes').getAttribute('aria-checked')).toBe('true')
    for (const name of ['git', 'files', 'notes']) {
      expect((rowSwitch(name) as HTMLButtonElement).disabled).toBe(false)
      expect(rowSwitch(name).getAttribute('aria-describedby') ?? '').not.toContain(HERO_SWITCH_ID)
    }
  })

  it('keeps the page-wide switch enabled when the only server is a declined per-project one', async () => {
    renderPage([server({ name: 'files', declined: true })])
    await screen.findByRole('switch', { name: `${M('share_toggle')} files` }, { timeout: 5000 })
    const hero = screen.getByRole('switch', { name: M('share_switch_label') })
    expect((hero as HTMLButtonElement).disabled).toBe(false)
  })
})
