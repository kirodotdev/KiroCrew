import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skillTrust: vi.fn(),
  revokeSkillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ProjectSkillsTrustList from '../components/ProjectSkillsTrustList'

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <ProjectSkillsTrustList />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  // clearAllMocks so a prior test's calls cannot satisfy a negative assertion.
  vi.clearAllMocks()
  mockApi.revokeSkillTrust.mockResolvedValue({})
})

describe('ProjectSkillsTrustList', () => {
  it('renders nothing when no folder has been trusted', async () => {
    mockApi.skillTrust.mockResolvedValue({ grants: [] })
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.skillTrust).toHaveBeenCalled())
    // An empty card would be permanent chrome for operators who never grant.
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the endpoint is unavailable', async () => {
    // Partial-mock / older backend: the method may be missing entirely.
    mockApi.skillTrust.mockResolvedValue(undefined)
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.skillTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('lists each trusted folder', async () => {
    mockApi.skillTrust.mockResolvedValue({
      grants: [
        { path: '/home/user/repo-a', exists: true },
        { path: '/home/user/repo-b', exists: true },
      ],
    })
    render(<Harness />)
    expect(await screen.findByText('/home/user/repo-a')).toBeInTheDocument()
    expect(screen.getByText('/home/user/repo-b')).toBeInTheDocument()
  })

  it('keeps a grant visible after its folder is gone', async () => {
    // Hiding it would make the grant invisible AND un-revokable, and a folder
    // recreated at the same path would inherit the old consent.
    mockApi.skillTrust.mockResolvedValue({
      grants: [{ path: '/home/user/deleted', exists: false }],
    })
    render(<Harness />)
    expect(await screen.findByText('/home/user/deleted')).toBeInTheDocument()
    expect(screen.getByText(/no longer exists/)).toBeInTheDocument()
  })

  it('withdraws the grant for the row that was clicked', async () => {
    mockApi.skillTrust.mockResolvedValue({
      grants: [
        { path: '/home/user/repo-a', exists: true },
        { path: '/home/user/repo-b', exists: true },
      ],
    })
    render(<Harness />)
    await screen.findByText('/home/user/repo-a')
    const buttons = screen.getAllByRole('button', { name: /Withdraw/ })
    fireEvent.click(buttons[1])
    await waitFor(() =>
      expect(mockApi.revokeSkillTrust).toHaveBeenCalledWith('/home/user/repo-b'),
    )
  })

  it('reports a failed withdrawal against that row only', async () => {
    mockApi.skillTrust.mockResolvedValue({
      grants: [
        { path: '/home/user/repo-a', exists: true },
        { path: '/home/user/repo-b', exists: true },
      ],
    })
    mockApi.revokeSkillTrust.mockRejectedValue(new Error('nope'))
    render(<Harness />)
    await screen.findByText('/home/user/repo-a')
    fireEvent.click(screen.getAllByRole('button', { name: /Withdraw/ })[0])
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Could not withdraw trust/)
    // One row failing must not mark the other as failed.
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('explains what withdrawing does', async () => {
    mockApi.skillTrust.mockResolvedValue({
      grants: [{ path: '/home/user/repo-a', exists: true }],
    })
    render(<Harness />)
    expect(await screen.findByText(/stop its skills loading/)).toBeInTheDocument()
  })
})
