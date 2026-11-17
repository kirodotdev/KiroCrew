// A shared server that waits for an approval shows on the Customize rail row
// (MCP Servers is a Customize tab; CapabilitiesPage.mcpShareBadge.test.tsx
// covers the tab).
//
// Contract under test:
// - the rail's badge map carries, under `capabilities`, how many servers set to share are
//   refused, read from the gateway's `share_refused`.
// - the row draws that count with a label that says what it counts.
// - sharing switched off, and a viewer the gateway refuses, count nothing.
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({ mcpGatewayStatus: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))
// The rail's other reads are not under test; each resolves empty.
vi.mock('../api/cronJobsQuery', () => ({
  cronJobsQuery: { queryKey: ['cron-jobs'], queryFn: async () => [] },
}))
vi.mock('../pages/apps/useAppsData', () => ({
  countUpdatables: () => 0,
  registryQueryFn: async () => ({}),
}))

import { ApiError } from '../api/apiError'
// `/all` registers the zh-CN catalog; the vitest setup registers English only.
import { i18next } from '../i18n/all'
import {
  MCP_GATEWAY_STATUS_QUERY_KEY,
  useMcpShareRefusedCount,
} from '../pages/overview/mcpShareRefusedQuery'
import { useRailBadges } from '../shell/nav/railBadges'
import { NavBadge } from '../shell/nav/navRow'
import '../surfaces/builtins'
import { renderHookWithProviders, renderWithProviders } from './helpers'

const WAIT = { timeout: 5000 }

const refused = {
  enabled: true,
  share_refused: { 'alpha-mcp': 'changed_needs_reapproval', 'beta-mcp': 'changed_needs_reapproval' },
}

/** Draws the count, on a client the test can read the query's state from. */
function Count() {
  return <output data-testid="count">{useMcpShareRefusedCount()}</output>
}

function mountCount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Count />
    </QueryClientProvider>,
  )
  const settled = (status: 'success' | 'error') =>
    waitFor(() => expect(qc.getQueryState(MCP_GATEWAY_STATUS_QUERY_KEY)?.status).toBe(status), WAIT)
  return { settled }
}

beforeEach(() => {
  mockApi.mcpGatewayStatus.mockReset()
})
afterEach(cleanup)

describe('the Customize rail badge for waiting MCP shares', () => {
  it('counts the servers set to share that wait for an approval', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(refused)
    const { result } = renderHookWithProviders(() => useRailBadges(0))
    await waitFor(() => expect(result.current.appBadges.capabilities).toBe(2), WAIT)
  })

  it('draws the count on the row and says what it counts', () => {
    renderWithProviders(<NavBadge navId="capabilities" collapsed appBadges={{ capabilities: 2 }} />)
    expect(screen.getByRole('status', { name: '2 servers waiting for approval' })).toBeInTheDocument()
  })

  // The expanded pill's `title` shows the label ALONE, and its aria-label puts
  // the count in front, so the label must read right both ways. A label that
  // opens with a counter word (件, 个, 개) reads wrong in the title.
  it.each([
    ['zh-CN', '等待批准的服务器'],
    ['ja', '承認待ちのサーバー'],
    ['ko', '승인 대기 중인 서버'],
    ['ru', 'серверы, ожидающие одобрения'],
  ])('says what it counts in %s, with and without the number', async (lang, label) => {
    const previous = i18next.language
    onTestFinished(() => i18next.changeLanguage(previous))
    await i18next.changeLanguage(lang)
    renderWithProviders(<NavBadge navId="capabilities" collapsed={false} appBadges={{ capabilities: 2 }} />)
    const pill = screen.getByLabelText(`2 ${label}`)
    expect(pill).toHaveAttribute('title', label)
  })

  it('draws nothing when no server waits', () => {
    renderWithProviders(<NavBadge navId="capabilities" collapsed appBadges={{ capabilities: 0 }} />)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('counts nothing while sharing is off', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue({ ...refused, enabled: false })
    const { settled } = mountCount()
    await settled('success')
    expect(screen.getByTestId('count')).toHaveTextContent('0')
  })

  it('counts nothing for a viewer the gateway refuses', async () => {
    mockApi.mcpGatewayStatus.mockRejectedValue(new ApiError(403, '{}'))
    const { settled } = mountCount()
    await settled('error')
    expect(screen.getByTestId('count')).toHaveTextContent('0')
  })
})
