// The MCP servers list names a config file it skipped because of its encoding.
//
// Contract under test: McpTab shows one line per file from
// GET /api/mcp/unreadable, with the file and the gateway's fixed reason, and a
// re-read of the servers list re-reads it, so a file re-saved as UTF-8 drops
// its line. A failed read says so instead of looking like "nothing skipped".
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  mcpUnreadableFiles: vi.fn(),
  mcpGatewayStatus: vi.fn(),
  mcpServers: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import { ApiError } from '../api/apiError'
import McpTab from '../pages/overview/McpTab'

const WAIT = { timeout: 5000 }
const REASON = 'it is saved as UTF-16; re-save it as UTF-8'

beforeEach(() => {
  // Only Date: the notice is keyed on the list's read time, so each read gets its own clock value.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-11-11T00:00:00Z'))
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.mcpGatewayStatus.mockResolvedValue({ enabled: false })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.acpBackends.mockResolvedValue({ backends: [] })
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

function mountTab(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpTab />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return qc
}

describe('McpUnreadableFilesNotice', () => {
  it('names each skipped file with its reason until a re-read of the list finds it readable', async () => {
    mockApi.mcpUnreadableFiles.mockResolvedValue({ files: [{ file: '~/.kiro/settings/mcp.json', reason: REASON }] })
    const qc = mountTab()

    const line = await screen.findByTestId('mcp-unreadable-file', undefined, WAIT)
    expect(line).toHaveTextContent(
      'Kiro Crew could not read ~/.kiro/settings/mcp.json because it is saved as UTF-16; re-save it as UTF-8. Its MCP servers are not listed.',
    )

    mockApi.mcpUnreadableFiles.mockResolvedValue({ files: [] })
    vi.setSystemTime(new Date('2026-11-11T00:00:05Z'))
    await qc.invalidateQueries({ queryKey: ['mcp-servers'] })
    await waitFor(() => expect(screen.queryByTestId('mcp-unreadable-file')).toBeNull(), WAIT)
  })

  it('says the check failed when the read fails', async () => {
    mockApi.mcpUnreadableFiles.mockRejectedValue(new ApiError(500, '{}'))
    mountTab()

    const notice = await screen.findByTestId('mcp-unreadable-files-error', undefined, WAIT)
    expect(notice).toHaveTextContent('Could not check which MCP config files were skipped.')
    expect(screen.queryByTestId('mcp-unreadable-file')).toBeNull()
  })
})
