import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  prompts: vi.fn(),
  promptDetail: vi.fn(),
  createPrompt: vi.fn(),
  updatePrompt: vi.fn(),
  deletePrompt: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi, ApiError: class ApiError extends Error {} }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../store', () => ({
  useAppDispatch: () => vi.fn(),
  useAppSelector: () => [],
}))

import PromptsTab from '../pages/overview/PromptsTab'

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><PromptsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('PromptsTab stacked confirmation', () => {
  beforeEach(() => {
    Object.values(mockApi).forEach(mock => mock.mockReset())
    mockApi.prompts.mockResolvedValue([])
  })

  it('disables the create modal while its discard confirmation owns focus', async () => {
    const user = userEvent.setup()
    renderTab()
    await user.click((await screen.findAllByRole('button', { name: 'Create New Prompt' }))[0])
    const outer = await screen.findByRole('dialog', { name: 'Create New Prompt' }) as HTMLElement
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'draft' } })

    await user.click(within(outer).getByRole('button', { name: 'Cancel' }))
    const confirmDialog = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })

    await waitFor(() => expect(outer.inert).toBe(true))
    expect(outer).toHaveAttribute('aria-hidden', 'true')
    await user.tab()
    expect(confirmDialog).toContainElement(document.activeElement as HTMLElement)
    expect(document.activeElement?.tagName).toBe('BUTTON')

    await user.click(within(confirmDialog).getByRole('button', { name: 'Keep editing' }))
    expect(confirmDialog).toBeInTheDocument()
    expect(outer.inert).toBe(true)
    await user.tab()
    expect(confirmDialog).toContainElement(document.activeElement as HTMLElement)
    await waitFor(() => expect(confirmDialog).not.toBeInTheDocument())
    await waitFor(() => expect(outer.inert).toBe(false))
    expect(outer).toContainElement(document.activeElement as HTMLElement)
  })
})
