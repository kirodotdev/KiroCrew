// MCP Management: the approval gate on a refused launch.
//
// Restored from main (McpManagement.launchApproval.test.tsx) after the one-switch
// redesign, in the page's current vocabulary. A launch the gateway refused (a
// changed command, or one declared outside the dashboard) runs nothing shared
// until the person approves it on its own button:
// - the row's switch reads off and, when pressed, writes NOTHING and reads
//   nothing: it moves focus to the row's Approve button
// - Approve re-sends stub=true pinned to the refusal's own expected_launch
// - a refusal with no approvable identity has no approve control, only a
//   Remove button that takes the mark off (stub=false), never an approval
// - an older gateway that sends no `launch_refused` keeps the plain states
//
// Every label is read from the catalog.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'

const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)
const rowName = (labelKey: string, name = 'alpha-mcp') => `${M(labelKey)} ${name}`

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

const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  stub: ['alpha-mcp'],
  stub_count: 1,
  running: true,
  ping_ok: true,
  supported: true,
  ...over,
})

const changed = {
  reason: 'changed_needs_reapproval',
  commands: [['npx', '-y', 'alpha-mcp@2']],
  envs: [['MODE=fast']],
  approved_commands: [['npx', '-y', 'alpha-mcp@1']],
  approved_envs: [['MODE=fast']],
  complete: true,
  expected_launch: 'a:b',
}

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

describe('a refused launch', () => {
  it('sends the switch of a refused row to its Approve button, reading nothing and writing nothing', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ launch_refused: { 'alpha-mcp': changed } }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const read = vi.spyOn(api, 'mcpGatewayLaunchPreview')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const control = await screen.findByRole('switch', { name: rowName('share_toggle') }, { timeout: 5000 })
    expect(control.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(control)

    const approve = screen.getByRole('button', { name: rowName('new_command_button') })
    expect(document.activeElement).toBe(approve)
    // The press is synchronous: it only moves focus, so nothing can be in flight.
    expect(read).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('approves by re-sending stub=true pinned to the refused launch', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ launch_refused: { 'alpha-mcp': changed } }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await screen.findByRole('button', { name: rowName('new_command_button') }, { timeout: 5000 }))
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'a:b'), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledTimes(1)
  })

  it('offers no approve control for a refusal with no approvable identity, only a Remove button', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ launch_refused: { 'alpha-mcp': { reason: 'changed_needs_reapproval', complete: false } } }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const control = await screen.findByRole('button', { name: rowName('remove_mark') }, { timeout: 5000 })
    expect(screen.queryByRole('switch', { name: rowName('share_toggle') })).toBeNull()
    expect(screen.queryByRole('button', { name: rowName('new_command_button') })).toBeNull()
    expect(screen.queryByRole('button', { name: rowName('approve_button') })).toBeNull()
    expect(screen.getByText(M('approval_not_approvable_here'))).toBeTruthy()
    fireEvent.click(control)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined), { timeout: 5000 })
  })

  it('closes the Turn on sharing confirm on Escape and turns nothing on', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    expect(enable).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('writes nothing when the Enable all list is cancelled', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ stub: [] }) as never)
    const declared = { strength: 'declared', recommendStub: true, recommendShare: true, reasons: [] }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ ...server, stub: false, in_allowlist: false, recommendation: declared }],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue({
      name: 'alpha-mcp', commands: [['/opt/a']], envs: [[]], complete: true, expected_launch: 'a-id',
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const all = await screen.findByRole('button', { name: M('enable_all') }, { timeout: 5000 })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)
    const list = await screen.findByRole('region', { name: M('share_all_list_aria') }, { timeout: 5000 })
    expect(list.textContent).toContain('alpha-mcp')
    fireEvent.click(screen.getByRole('button', { name: M('share_all_cancel') }))
    await waitFor(() => expect(screen.queryByRole('region', { name: M('share_all_list_aria') })).toBeNull(), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('keeps the plain state when the gateway sends no launch_refused', async () => {
    // An older gateway: the field is absent, which must read as "nothing refused".
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)

    mount()
    const control = await screen.findByRole('switch', { name: rowName('share_toggle') }, { timeout: 5000 })
    expect(control.getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText(M('state_shared'), { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText(M('state_needs_reapproval'))).toBeNull()
    expect(screen.queryByRole('button', { name: rowName('new_command_button') })).toBeNull()
    expect(screen.queryByRole('button', { name: M('review_approve') })).toBeNull()
  })
})
