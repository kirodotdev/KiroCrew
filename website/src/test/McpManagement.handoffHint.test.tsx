/**
 * MCP Management withholds "Ask the agent" while the sibling MCP Servers list
 * holds unapplied edits (the hand-off navigates to /chat and would drop them).
 * The notice then says why, in place of the button, and the button comes back
 * once the edits are applied or discarded.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { McpManagement } from '../pages/settings/McpManagement'
import { McpStagedEditsProvider, useSetMcpStagedEdits } from '../pages/connections/mcpStagedEditsContext'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

const M = (key: string) => i18nT(`pages.mcpManagement.${key}`)
const ASK = /ask the agent/i

/** Stands in for the MCP Servers list: publishes "edits staged" on demand. */
function StageControls() {
  const setStaged = useSetMcpStagedEdits()
  return (
    <>
      <button type="button" onClick={() => setStaged(true)}>stage edits</button>
      <button type="button" onClick={() => setStaged(false)}>apply edits</button>
    </>
  )
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpStagedEditsProvider>
          <StageControls />
          <McpManagement />
        </McpStagedEditsProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({ using_now_bytes: null, with_sharing_bytes: null, open_chats: 0, stale_chats: [] } as never)
  answerBatchPreviewsPerName()
  vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
    enabled: false, stub: [], stub_count: 0, running: false, ping_ok: false, supported: true,
  } as never)
  // A failed list read: a page-wide notice that offers the hand-off.
  vi.spyOn(api, 'mcpGatewayServers').mockRejectedValue(new Error('boom'))
})
afterEach(cleanup)

describe('McpManagement hand-off hint', () => {
  it('names what brings "Ask the agent" back while edits are staged', async () => {
    mount()
    const text = await screen.findByText(M('servers_failed'))
    const notice = text.closest('[role="alert"]') as HTMLElement
    expect(within(notice).getByRole('button', { name: ASK })).toBeTruthy()
    expect(notice).not.toHaveTextContent(M('handoff_hint_staged_edits'))

    act(() => { screen.getByText('stage edits').click() })
    expect(within(notice).queryByRole('button', { name: ASK })).toBeNull()
    expect(notice).toHaveTextContent(M('handoff_hint_staged_edits'))

    act(() => { screen.getByText('apply edits').click() })
    expect(within(notice).getByRole('button', { name: ASK })).toBeTruthy()
    expect(notice).not.toHaveTextContent(M('handoff_hint_staged_edits'))
  })
})
