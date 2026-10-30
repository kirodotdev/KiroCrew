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

  it('puts the config editor directly under the card title', () => {
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const title = screen.getByRole('heading', { level: 3 })
    expect(title.nextElementSibling).toBe(screen.getByLabelText('Agent config JSON'))
  })

  it('renders invalid JSON as an inline validation status', async () => {
    render(<AgentCfgTab />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByDisplayValue(/claude-3/)).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Agent config JSON'), { target: { value: '{' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
  })

  it('renders a save rejection through ErrorNotice without discarding the draft', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.saveAgentConfig).mockRejectedValueOnce(new Error('save refused'))
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.change(editor, { target: { value: '{\n  "model": "edited"\n}' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByTestId('agent-config-save-error')).toHaveTextContent('save refused')
    expect(screen.getByDisplayValue(/edited/)).toBeInTheDocument()
  })
})
