import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RegistryManager from '../components/RegistryManager'

const mockListRegistries = vi.fn()
const mockUpdateRegistries = vi.fn()
const mockRefreshRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listRegistries: (...args: unknown[]) => mockListRegistries(...args),
    updateRegistries: (...args: unknown[]) => mockUpdateRegistries(...args),
    refreshRegistries: (...args: unknown[]) => mockRefreshRegistries(...args),
  },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

// A deferred promise so we can assert pending UI before resolving.
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

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
        { name: 'Identity Services', repo: 'IdentityApps', branch: 'main' },
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
    expect(screen.getByPlaceholderText(/app-registry/)).toBeInTheDocument()
  })

  it('shows main as the branch placeholder (backend owns the default)', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    // Field is left empty and submits empty so the backend derives 'main';
    // the placeholder communicates that default to the user.
    expect(screen.getByPlaceholderText('main')).toBeInTheDocument()
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

  it('rejects junk / shell metacharacters', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/app-registry/)
    fireEvent.change(repoInput, { target: { value: 'foo; rm -rf /' } })
    fireEvent.click(screen.getByText('Add Registry'))
    await waitFor(() => {
      expect(screen.getByText(/git URL or an alphanumeric name/)).toBeInTheDocument()
    })
    expect(mockUpdateRegistries).not.toHaveBeenCalled()
  })

  it('rejects plaintext http:// URLs (https-only, mirrors backend)', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/app-registry/)
    fireEvent.change(repoInput, { target: { value: 'http://github.com/org/app-registry' } })
    fireEvent.click(screen.getByText('Add Registry'))
    await waitFor(() => {
      expect(screen.getByText(/git URL or an alphanumeric name/)).toBeInTheDocument()
    })
    expect(mockUpdateRegistries).not.toHaveBeenCalled()
  })

  it('keeps the add form populated when the backend rejects the add', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    // Simulate a backend 400 (e.g. name/branch validation) AFTER submit.
    mockUpdateRegistries.mockRejectedValue(new Error('Invalid registry'))
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/app-registry/) as HTMLInputElement
    fireEvent.change(repoInput, { target: { value: 'https://github.com/org/app-registry' } })
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => {
      expect(mockUpdateRegistries).toHaveBeenCalled()
    })
    // The error surfaces AND the form stays open with the user's input intact,
    // so they can correct it without re-typing.
    await waitFor(() => {
      expect(screen.getByText('Invalid registry')).toBeInTheDocument()
    })
    const stillThere = screen.getByPlaceholderText(/app-registry/) as HTMLInputElement
    expect(stillThere).toBeInTheDocument()
    expect(stillThere.value).toBe('https://github.com/org/app-registry')
  })

  it('accepts a full git URL on add', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/app-registry/)
    fireEvent.change(repoInput, { target: { value: 'https://github.com/org/app-registry' } })
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => {
      expect(mockUpdateRegistries).toHaveBeenCalledWith([
        { name: '', repo: 'https://github.com/org/app-registry', branch: '' },
      ])
    })
  })

  it('surfaces the trust-grant notice when the backend reports a newly trusted host', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    // The backend admits the host and echoes it back as a genuine trust grant.
    mockUpdateRegistries.mockResolvedValue({
      ok: true,
      registries: [{ name: '', repo: 'https://github.com/org/app-registry', branch: 'main' }],
      newlyTrustedHosts: ['github.com'],
    })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
      target: { value: 'https://github.com/org/app-registry' },
    })
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    // The owner who clicked Add must see the host they just began trusting.
    await waitFor(() => {
      expect(screen.getByText(/now trusting apps from github\.com/i)).toBeInTheDocument()
    })
  })

  it('shows no trust notice when the backend reports no newly trusted host', async () => {
    mockListRegistries.mockResolvedValue({ registries: [] })
    mockUpdateRegistries.mockResolvedValue({
      ok: true,
      registries: [{ name: 'MyOrg', repo: 'MyOrgApps', branch: 'main' }],
      newlyTrustedHosts: [],
    })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    fireEvent.change(screen.getByPlaceholderText(/app-registry/), { target: { value: 'MyOrgApps' } })
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => expect(mockUpdateRegistries).toHaveBeenCalled())
    expect(screen.queryByText(/now trusting apps from/i)).not.toBeInTheDocument()
  })

  it('calls updateRegistries on successful bare-name add', async () => {    mockListRegistries.mockResolvedValue({ registries: [] })
    mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [{ name: 'MyOrg', repo: 'MyOrgApps', branch: 'main' }] })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Add Registry'))
    fireEvent.click(screen.getByText('Add Registry'))
    const repoInput = screen.getByPlaceholderText(/app-registry/)
    fireEvent.change(repoInput, { target: { value: 'MyOrgApps' } })
    const buttons = screen.getAllByText('Add Registry')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => {
      // name/branch sent empty so the BACKEND derives the safe slug + 'main'.
      expect(mockUpdateRegistries).toHaveBeenCalledWith([
        { name: '', repo: 'MyOrgApps', branch: '' },
      ])
    })
  })

  it('Sync Apps button calls refreshRegistries with no repo', async () => {
    mockListRegistries.mockResolvedValue({
      registries: [{ name: 'Identity', repo: 'IdentityApps', branch: 'main' }],
    })
    mockRefreshRegistries.mockResolvedValue({ ok: true, refreshed: ['IdentityApps'], apps: 3, lastSyncedAt: new Date().toISOString() })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Sync Apps'))
    fireEvent.click(screen.getByLabelText('Sync registry apps'))
    await waitFor(() => {
      expect(mockRefreshRegistries).toHaveBeenCalledWith(undefined)
    })
    // Last-synced time surfaces after success
    await waitFor(() => {
      expect(screen.getByText(/Last synced/)).toBeInTheDocument()
    })
  })

  it('surfaces a warning when a refresh partially fails', async () => {
    mockListRegistries.mockResolvedValue({
      registries: [{ name: 'Identity', repo: 'IdentityApps', branch: 'main' }],
    })
    mockRefreshRegistries.mockResolvedValue({
      ok: false,
      refreshed: [],
      failed: ['Identity'],
      results: [{ name: 'Identity', ok: false }],
      apps: 2,
      lastSyncedAt: new Date().toISOString(),
    })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Sync Apps'))
    fireEvent.click(screen.getByLabelText('Sync registry apps'))
    await waitFor(() => {
      expect(screen.getByText(/Could not refresh: Identity/)).toBeInTheDocument()
    })
  })

  it('per-row refresh calls refreshRegistries with that repo', async () => {
    mockListRegistries.mockResolvedValue({
      registries: [{ name: 'Identity', repo: 'IdentityApps', branch: 'main' }],
    })
    mockRefreshRegistries.mockResolvedValue({ ok: true, refreshed: ['IdentityApps'], apps: 1, lastSyncedAt: new Date().toISOString() })
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Identity'))
    fireEvent.click(screen.getByLabelText('Refresh Identity registry'))
    await waitFor(() => {
      expect(mockRefreshRegistries).toHaveBeenCalledWith('IdentityApps')
    })
  })

  it('renders pending state while syncing', async () => {
    mockListRegistries.mockResolvedValue({
      registries: [{ name: 'Identity', repo: 'IdentityApps', branch: 'main' }],
    })
    const d = deferred<{ ok: boolean; refreshed: string[]; apps: number; lastSyncedAt: string }>()
    mockRefreshRegistries.mockReturnValue(d.promise)
    render(<RegistryManager />, { wrapper: Wrapper })
    await waitFor(() => screen.getByText('Sync Apps'))
    fireEvent.click(screen.getByLabelText('Sync registry apps'))
    await waitFor(() => {
      expect(screen.getByText('Syncing…')).toBeInTheDocument()
    })
    d.resolve({ ok: true, refreshed: ['IdentityApps'], apps: 1, lastSyncedAt: new Date().toISOString() })
    await waitFor(() => {
      expect(screen.getByText('Sync Apps')).toBeInTheDocument()
    })
  })
})
