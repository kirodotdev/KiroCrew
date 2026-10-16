import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'
import type { AcpBackendProbe } from '../api/client'
import { ApiError } from '../api/apiError'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
// The modal fires its own queries and has its own suite; not under test here.
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

// Every assertion waits on the mocked queries resolving and the table rendering.
const ROW_WAIT = { timeout: 5000 }

const RULE = /stops every tool on the same server/
const EXCEPTION = /One exception/

const server: McpServer = {
  name: 'kirocrew-core',
  command: 'kirocrew-core-cmd',
  status: 'ok',
  source: 'kirocrew',
  enabled: true,
  tools: ['send_message', 'work_report'],
}

/** One `/api/acp-backends` row; only `mcp` matters to this surface. */
function probeRow(id: string, perToolDeny: string, policyId = id): AcpBackendProbe {
  return {
    id,
    policy_id: policyId,
    selectable: true,
    independent_setup: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    // The server ships its own classification beside the reach
    // (`backend_mcp_ability.COSTS_WHOLE_SERVER`).
    mcp: {
      per_tool_deny: perToolDeny,
      costs_whole_server: perToolDeny === 'whole-server' || perToolDeny === 'per-call',
      ineffective: [],
    },
  } as AcpBackendProbe
}

const BACKENDS = {
  backends: [
    probeRow('goose', 'whole-server', 'Goose'),
    probeRow('codex', 'per-call', 'Codex'),
    probeRow('claude', 'settings-file'),
  ],
}

let qc: QueryClient

function renderTab() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Open the server's tool list and return the row it sits in. */
async function openTools(): Promise<HTMLElement> {
  const row = (await screen.findByText('kirocrew-core', { selector: 'code' }, ROW_WAIT)).closest('tr')!
  fireEvent.click(within(row).getByRole('button', { name: /2\s+tools/i }))
  await within(row).findByRole('button', { name: 'send_message' }, ROW_WAIT)
  return row
}

/**
 * Wait until the queries the line reads have settled, whatever they answered, so
 * an absence asserted afterwards is the component's answer and not a race with a
 * pending fetch. `keys` names the ones expected to settle in that test.
 */
async function settled(keys: string[] = ['kirocrewConfig', 'acpBackends']) {
  await waitFor(() => {
    for (const key of keys) expect(['success', 'error']).toContain(qc.getQueryState([key])?.status)
  }, ROW_WAIT)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.mcpServers.mockResolvedValue([server])
  mockApi.acpBackends.mockResolvedValue(BACKENDS)
})

describe('McpTab: what turning one tool off costs on the harness in use (#11969)', () => {
  it('states the rule above the tools on a whole-server harness, before any click', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'goose' } })
    renderTab()
    const row = await openTools()
    const reach = await within(row).findByTestId('mcp-deny-reach', undefined, ROW_WAIT)
    const rule = within(reach).getByText(RULE)
    expect(rule.textContent).toContain('Goose')
    expect(rule).toHaveClass('text-text-strong')
    // A whole-server reach has no exception.
    expect(within(reach).queryByText(EXCEPTION)).toBeNull()
    // Above the list, so it is read before a tool is clicked.
    const firstTool = within(row).getByRole('button', { name: 'send_message' })
    expect(reach.compareDocumentPosition(firstTool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('adds the per-call exception, muted, right under the rule', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'codex' } })
    renderTab()
    const row = await openTools()
    const reach = await within(row).findByTestId('mcp-deny-reach', undefined, ROW_WAIT)
    const rule = within(reach).getByText(RULE)
    const exception = within(reach).getByText(EXCEPTION)
    expect(rule.textContent).toContain('Codex')
    expect(exception.textContent).toContain('kirocrew-core')
    expect(exception).toHaveClass('text-muted')
    expect(rule.compareDocumentPosition(exception) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says nothing on a harness whose tool-off stops only that tool', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    renderTab()
    const row = await openTools()
    await settled()
    expect(within(row).queryByTestId('mcp-deny-reach')).toBeNull()
    expect(screen.queryByText(RULE)).toBeNull()
  })

  it('says nothing when the probe is refused (403 for a non-owner)', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'goose' } })
    mockApi.acpBackends.mockRejectedValue(new ApiError(403, 'forbidden', '{}'))
    renderTab()
    const row = await openTools()
    await settled()
    expect(within(row).queryByTestId('mcp-deny-reach')).toBeNull()
  })

  it('says nothing while the config is still loading', async () => {
    // The probe has answered; without the config there is no harness to name.
    mockApi.kirocrewConfig.mockReturnValue(new Promise(() => {}))
    renderTab()
    const row = await openTools()
    await settled(['acpBackends'])
    expect(within(row).queryByTestId('mcp-deny-reach')).toBeNull()
  })

  it('stays inside the tool list: a closed list shows nothing', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'goose' } })
    renderTab()
    await screen.findByText('kirocrew-core', { selector: 'code' }, ROW_WAIT)
    await settled()
    expect(screen.queryByTestId('mcp-deny-reach')).toBeNull()
  })

  it('only declares: the tool still turns off on a whole-server harness', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'goose' } })
    renderTab()
    const row = await openTools()
    await within(row).findByTestId('mcp-deny-reach', undefined, ROW_WAIT)
    const tool = within(row).getByRole('button', { name: 'send_message' })
    expect(tool).toBeEnabled()
    fireEvent.click(tool)
    await waitFor(() => expect(tool).toHaveClass('line-through'))
  })
})
