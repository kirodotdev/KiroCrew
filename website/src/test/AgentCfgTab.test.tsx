import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentCfgTab from '../pages/overview/AgentCfgTab'
import { api } from '../api/client'

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
  beforeEach(() => {
    vi.clearAllMocks()
    qc.clear()
    vi.mocked(api.agentConfig).mockReset().mockResolvedValue({ model: 'claude-3', tools: ['bash'] })
    vi.mocked(api.saveAgentConfig).mockReset().mockResolvedValue({ ok: true })
  })

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

  it('keeps invalid JSON in the editor and makes its validation notice dismissible', async () => {
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.change(editor, { target: { value: '{' } })
    fireEvent.click(screen.getByText('Save'))
    const notice = await screen.findByTestId('agent-config-validation-notice')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('Invalid JSON')
    expect(api.saveAgentConfig).not.toHaveBeenCalled()
    expect(editor).toHaveValue('{')
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('agent-config-validation-notice')).toBeNull()
    expect(editor).toHaveValue('{')
  })

  it('clears validation on a valid retry and preserves edits during a successful save', async () => {
    let finish!: (value: { ok: boolean }) => void
    const pending = new Promise<{ ok: boolean }>(resolve => { finish = resolve })
    vi.mocked(api.saveAgentConfig).mockReturnValueOnce(pending)
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.change(editor, { target: { value: '{' } })
    fireEvent.click(screen.getByText('Save'))
    await screen.findByTestId('agent-config-validation-notice')

    fireEvent.change(editor, { target: { value: '{"model":"edited"}' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(api.saveAgentConfig).toHaveBeenCalledWith({ model: 'edited' }))
    expect(screen.queryByTestId('agent-config-validation-notice')).toBeNull()
    fireEvent.change(editor, { target: { value: '' } })
    vi.mocked(api.agentConfig).mockResolvedValue({ model: 'server copy' })
    await act(async () => { finish({ ok: true }); await pending })
    await screen.findByText('Saved')
    await waitFor(() => expect(api.agentConfig).toHaveBeenCalledTimes(2))
    await act(async () => { await qc.refetchQueries({ queryKey: ['agent-config'] }) })
    expect(editor).toHaveValue('')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps a failed save local, dismissible, and retryable without a navigation hand-off', async () => {
    vi.mocked(api.saveAgentConfig).mockRejectedValueOnce(new Error('save refused'))
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    const draft = '{"model":"edited"}'
    fireEvent.change(editor, { target: { value: draft } })
    fireEvent.click(screen.getByText('Save'))
    const notice = await screen.findByTestId('agent-config-save-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Save failed')
    expect(editor).toHaveValue(draft)
    expect(within(notice).queryByRole('button', { name: /Ask/i })).toBeNull()
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('agent-config-save-error')).toBeNull()
    expect(editor).toHaveValue(draft)

    vi.mocked(api.saveAgentConfig).mockRejectedValueOnce(new Error('still refused'))
    fireEvent.click(screen.getByText('Save'))
    await screen.findByTestId('agent-config-save-error')
    let finish!: (value: { ok: boolean }) => void
    const pending = new Promise<{ ok: boolean }>(resolve => { finish = resolve })
    vi.mocked(api.saveAgentConfig).mockReturnValueOnce(pending)
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(api.saveAgentConfig).toHaveBeenCalledTimes(3))
    expect(screen.queryByTestId('agent-config-save-error')).toBeNull()
    await act(async () => { finish({ ok: true }); await pending })
    await screen.findByText('Saved')
    expect(editor).toHaveValue(draft)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('replaces an old save failure with validation when the next draft is invalid', async () => {
    vi.mocked(api.saveAgentConfig).mockRejectedValueOnce(new Error('refused'))
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.click(screen.getByText('Save'))
    await screen.findByTestId('agent-config-save-error')
    fireEvent.change(editor, { target: { value: '' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
    expect(screen.queryByTestId('agent-config-save-error')).toBeNull()
    expect(api.saveAgentConfig).toHaveBeenCalledTimes(1)
    expect(editor).toHaveValue('')
  })

  it('reports a pending save failure after an invalid draft resets the mutation', async () => {
    let fail!: (error: Error) => void
    const pending = new Promise<{ ok: boolean }>((_, reject) => { fail = reject })
    vi.mocked(api.saveAgentConfig).mockReturnValueOnce(pending)
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveAgentConfig).toHaveBeenCalledTimes(1))

    fireEvent.change(editor, { target: { value: '{' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
    await act(async () => { fail(new Error('save refused')); await pending.catch(() => {}) })
    const notice = await screen.findByTestId('agent-config-save-error')
    expect(notice).toHaveTextContent('Save failed')
    expect(screen.getByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
    expect(api.saveAgentConfig).toHaveBeenCalledTimes(1)
    expect(editor).toHaveValue('{')
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('agent-config-save-error')).toBeNull()
    expect(screen.getByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
  })

  it('preserves an empty draft edited before the initial config query completes', async () => {
    let finish!: (value: { model: string }) => void
    const pending = new Promise<{ model: string }>(resolve => { finish = resolve })
    vi.mocked(api.agentConfig).mockReturnValueOnce(pending)
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = screen.getByLabelText('Agent config JSON')
    fireEvent.change(editor, { target: { value: '{draft' } })
    fireEvent.change(editor, { target: { value: '' } })
    await act(async () => { finish({ model: 'loaded' }); await pending })
    await act(async () => { await qc.refetchQueries({ queryKey: ['agent-config'] }) })
    expect(editor).toHaveValue('')
  })

  it('clears the saved label when the next save fails JSON validation', async () => {
    render(<AgentCfgTab />, { wrapper: Wrapper })
    const editor = await screen.findByDisplayValue(/claude-3/)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Saved' })
    fireEvent.change(editor, { target: { value: '{' } })
    fireEvent.click(screen.getByRole('button', { name: 'Saved' }))
    expect(await screen.findByTestId('agent-config-validation-notice')).toHaveTextContent('Invalid JSON')
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Saved' })).toBeNull()
    expect(api.saveAgentConfig).toHaveBeenCalledTimes(1)
    expect(editor).toHaveValue('{')
  })
})
