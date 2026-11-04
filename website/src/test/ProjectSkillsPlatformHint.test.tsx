import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ProjectSkillsPlatformHint from '../components/ProjectSkillsPlatformHint'
import { ApiError } from '../api/apiError'

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <ProjectSkillsPlatformHint />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ProjectSkillsPlatformHint', () => {
  it('shows one muted line when the gateway says project skills cannot load here', async () => {
    mockApi.skillTrust.mockResolvedValue({ grants: [], platform_unsupported: true })
    render(<Harness />)
    const hint = await screen.findByTestId('project-skills-platform-hint')
    expect(hint).toHaveTextContent('Project skills (.kiro/skills) do not load on Windows yet.')
    expect(hint).toHaveClass('text-muted')
  })

  it.each([
    ['the flag is false', { grants: [], platform_unsupported: false }],
    ['an older gateway omits the flag', { grants: [] }],
    ['the endpoint is unavailable', undefined],
  ])('renders nothing when %s', async (_label, payload) => {
    mockApi.skillTrust.mockResolvedValue(payload)
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.skillTrust).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('re-reads a cached false on mount, so a project opened since shows the hint', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Fresh cache entry from before any chat had a project, well inside staleTime.
    qc.setQueryData(['skill-trust'], { grants: [], platform_unsupported: false })
    mockApi.skillTrust.mockResolvedValue({ grants: [], platform_unsupported: true })
    render(
      <QueryClientProvider client={qc}>
        <ProjectSkillsPlatformHint />
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('project-skills-platform-hint')).toBeInTheDocument()
    expect(mockApi.skillTrust).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['a non-owner', new ApiError(403, 'owner required', JSON.stringify({ code: 'dashboard_owner_required' }))],
    ['a lapsed session', new ApiError(401, 'sign in again')],
  ])('stays quiet when the read is refused for %s', async (_label, refusal) => {
    mockApi.skillTrust.mockRejectedValue(refusal)
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.skillTrust).toHaveBeenCalled())
    // Let the rejection settle before asserting nothing rendered.
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('project-skills-platform-hint-error')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('still reports a forbidden read that is not the owner refusal', async () => {
    mockApi.skillTrust.mockRejectedValue(new ApiError(403, 'blocked', JSON.stringify({ code: 'other' })))
    render(<Harness />)
    expect(await screen.findByTestId('project-skills-platform-hint-error')).toBeInTheDocument()
  })

  it('says so through ErrorNotice when the status read fails', async () => {
    mockApi.skillTrust.mockRejectedValue(new Error('502 Bad Gateway'))
    render(<Harness />)
    const notice = await screen.findByTestId('project-skills-platform-hint-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Could not check whether project skills can load here.')
    expect(screen.queryByTestId('project-skills-platform-hint')).toBeNull()
  })
})
