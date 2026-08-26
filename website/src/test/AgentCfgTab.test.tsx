import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentCfgTab from '../pages/overview/AgentCfgTab'

vi.mock('../api/client', () => ({
  api: {
    agentConfig: vi.fn().mockResolvedValue({ model: 'claude-3', tools: ['bash'] }),
    saveAgentConfig: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

describe('AgentCfgTab', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  it('loads and displays agent config JSON', async () => {
    render(<AgentCfgTab />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByDisplayValue(/claude-3/)).toBeInTheDocument()
    })
  })

  it('saves config on button click', async () => {
    const { api } = await import('../api/client')
    render(<AgentCfgTab />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByDisplayValue(/claude-3/)).toBeInTheDocument())
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      expect(api.saveAgentConfig).toHaveBeenCalled()
    })
  })
})
