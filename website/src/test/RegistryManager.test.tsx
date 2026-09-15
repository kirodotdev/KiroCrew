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

  // --- SSH URL parity tests: frontend isValidRepo must accept the same forms
  // as the backend _SAFE_SSH_URL_RE (which now allows userless ssh://).
  describe('ssh URL validation parity', () => {
    it('accepts userless ssh:// URL (e.g. ssh://git.example.com/pkg/Name)', async () => {
      mockListRegistries.mockResolvedValue({ registries: [] })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'ssh://git.example.com/pkg/MyApps' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: '', repo: 'ssh://git.example.com/pkg/MyApps', branch: '' },
        ])
      })
    })

    it('accepts ssh:// URL with user@ (e.g. ssh://user@git.example.com/pkg/Name)', async () => {
      mockListRegistries.mockResolvedValue({ registries: [] })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'ssh://user@git.example.com/pkg/MyApps' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: '', repo: 'ssh://user@git.example.com/pkg/MyApps', branch: '' },
        ])
      })
    })

    it('accepts ssh:// URL with port (e.g. ssh://git.example.com:22/pkg/Name)', async () => {
      mockListRegistries.mockResolvedValue({ registries: [] })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'ssh://git.example.com:22/pkg/MyApps' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: '', repo: 'ssh://git.example.com:22/pkg/MyApps', branch: '' },
        ])
      })
    })

    it('rejects plaintext http:// URL (mirrors backend MITM defense)', async () => {
      mockListRegistries.mockResolvedValue({ registries: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'http://github.com/org/app-registry' },
      })
      fireEvent.click(screen.getAllByText('Add Registry').pop()!)
      await waitFor(() => {
        expect(screen.getByText(/git URL or an alphanumeric name/)).toBeInTheDocument()
      })
      expect(mockUpdateRegistries).not.toHaveBeenCalled()
    })

    it('accepts scp-style git@host:path form', async () => {
      mockListRegistries.mockResolvedValue({ registries: [] })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'git@github.com:org/app-registry.git' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: '', repo: 'git@github.com:org/app-registry.git', branch: '' },
        ])
      })
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

  describe('build-pinned registries', () => {
    const PINNED = { name: 'Official', repo: 'https://forge.example.com/org/registry.git', branch: 'main', trust: 'owner' }

    it('renders a pinned registry with no remove control', async () => {
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Official')).toBeInTheDocument())
      expect(screen.getByText('Included with this installation')).toBeInTheDocument()
      // A remove button would appear to work and be undone by the next read.
      expect(screen.queryByLabelText('Remove Official registry')).toBeNull()
    })

    it('does not show the empty state when only a pinned registry exists', async () => {
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Official')).toBeInTheDocument())
      expect(screen.queryByText('No external registries')).toBeNull()
    })

    it('refuses to add a repo a pinned registry already owns', async () => {
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), { target: { value: PINNED.repo } })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(screen.getByText(/already exists/)).toBeInTheDocument()
      })
      expect(mockUpdateRegistries).not.toHaveBeenCalled()
    })

    it('never sends a pinned registry back on the replace-all PUT', async () => {
      // The PUT writes the operator's own config. Including a pinned row would
      // persist it there, where a later build change could no longer move it.
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [], newlyTrustedHosts: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'https://forge.example.com/org/other.git' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: '', repo: 'https://forge.example.com/org/other.git', branch: '' },
        ])
      })
    })

    it('does not invent a trust tier for an operator row on a replace-all PUT', async () => {
      // The backend resolves the trusted tier only from what the build supplies,
      // because config.json is agent-writable — so an operator row always reads
      // `index` and there is no tier for this round-trip to preserve. What must
      // hold is that adding a row does not invent one.
      mockListRegistries.mockResolvedValue({
        registries: [{ name: 'Mine', repo: 'https://forge.example.com/org/mine.git', branch: 'main', trust: 'index' }],
      })
      mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [], newlyTrustedHosts: [] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'https://forge.example.com/org/second.git' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(mockUpdateRegistries).toHaveBeenCalledWith([
          { name: 'Mine', repo: 'https://forge.example.com/org/mine.git', branch: 'main', trust: 'index' },
          { name: '', repo: 'https://forge.example.com/org/second.git', branch: '' },
        ])
      })
    })

    it('refuses a NAME a pinned registry already owns', async () => {
      // The backend merge drops a same-named operator row, so accepting it here
      // would render a row whose apps never appear and whose refresh 404s.
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => screen.getByText('Add Registry'))
      fireEvent.click(screen.getByText('Add Registry'))
      fireEvent.change(screen.getByPlaceholderText(/Identity Services/i), { target: { value: 'Official' } })
      fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
        target: { value: 'https://forge.example.com/org/mine.git' },
      })
      const buttons = screen.getAllByText('Add Registry')
      fireEvent.click(buttons[buttons.length - 1])
      await waitFor(() => {
        expect(screen.getByText(/name of a registry this build provides/)).toBeInTheDocument()
      })
      expect(mockUpdateRegistries).not.toHaveBeenCalled()
    })

    it('keeps Sync Apps reachable when only pinned rows exist', async () => {
      // A deployment whose users never add their own registry is exactly this
      // feature's audience; gating Sync on operator rows hid it from them.
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Official')).toBeInTheDocument())
      expect(screen.getByLabelText('Sync registry apps')).toBeInTheDocument()
    })

    it('offers the read-only open-repository control on a pinned row', async () => {
      mockListRegistries.mockResolvedValue({ registries: [], pinned: [PINNED] })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Official')).toBeInTheDocument())
      expect(
        screen.getByLabelText(`Open ${PINNED.repo} repository`),
      ).toBeInTheDocument()
    })
    it('gives the owner trust tier a text counterpart, not just an icon', async () => {
      // The owner tier is the state that clones with the operator's git
      // credentials. An icon swap alone is undecodable and silent to a screen
      // reader, so the state must also be readable.
      mockListRegistries.mockResolvedValue({
        registries: [],
        pinned: [{ ...PINNED, trust: 'owner' }],
      })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Trusted source')).toBeInTheDocument())
    })

    it('does not claim trusted source for the default index tier', async () => {
      mockListRegistries.mockResolvedValue({
        registries: [],
        pinned: [{ ...PINNED, trust: 'index' }],
      })
      render(<RegistryManager />, { wrapper: Wrapper })
      await waitFor(() => expect(screen.getByText('Official')).toBeInTheDocument())
      expect(screen.queryByText('Trusted source')).toBeNull()
    })
  })
})
