import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RegistryManager from '../components/RegistryManager'

const mockListRegistries = vi.fn()
const mockUpdateRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listRegistries: (...args: unknown[]) => mockListRegistries(...args),
    updateRegistries: (...args: unknown[]) => mockUpdateRegistries(...args),
  },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

describe('RegistryManager', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    qc.clear()
  })

  it('shows empty state when no registries configured', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByText('No external registries')).toBeInTheDocument()
    })
  })

  it('renders configured registries', async () => {
    mockListRegistries.mockResolvedValue({
      registries: [
        { name: 'Identity Services', repo: 'IdentityApps', branch: 'mainline' },
      ],
    })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByText('Identity Services')).toBeInTheDocument()
      expect(screen.getByText('IdentityApps')).toBeInTheDocument()
    })
  })

  it('shows add form when Add Registry is clicked', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    expect(screen.getByPlaceholderText(/kirocrew-app-registry/)).toBeInTheDocument()
  })

  it('validates empty repo on add', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    // Click add without filling repo
    fireEvent.click(screen.getByText('Add Registry'))
    await waitFor(() => {
      expect(screen.getByText('Repo name is required')).toBeInTheDocument()
    })
  })

  it('validates invalid repo characters', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/kirocrew-app-registry/)
    fireEvent.change(repoInput, { target: { value: '../evil' } })
    fireEvent.click(screen.getByText('Add Registry'))
    await waitFor(() => {
      expect(screen.getByText(/alphanumeric/)).toBeInTheDocument()
    })
  })

  it('calls updateRegistries on successful add', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [{ name: 'MyOrg', repo: 'MyOrgApps', branch: 'mainline' }] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/kirocrew-app-registry/)
    fireEvent.change(repoInput, { target: { value: 'MyOrgApps' } })
    // Find the submit button (second "Add Registry" text)
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => {
      expect(mockUpdateRegistries).toHaveBeenCalledWith([
        { name: 'MyOrgApps', repo: 'MyOrgApps', branch: 'mainline' },
      ])
    })
  })
})
