import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  uninstalledBuiltinSkills: vi.fn(),
  restoreBuiltinSkill: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import UninstalledBuiltinSkillsList from '../components/UninstalledBuiltinSkillsList'

function Harness({ askAgent }: { askAgent?: boolean } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <UninstalledBuiltinSkillsList askAgent={askAgent} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.restoreBuiltinSkill.mockResolvedValue({ ok: true })
})

describe('UninstalledBuiltinSkillsList', () => {
  it('renders nothing when no built-in skill is uninstalled', async () => {
    mockApi.uninstalledBuiltinSkills.mockResolvedValue({ uninstalled: [] })
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.uninstalledBuiltinSkills).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the endpoint is unavailable', async () => {
    mockApi.uninstalledBuiltinSkills.mockResolvedValue(undefined)
    const { container } = render(<Harness />)
    await waitFor(() => expect(mockApi.uninstalledBuiltinSkills).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('lists each uninstalled built-in and restores the one clicked', async () => {
    mockApi.uninstalledBuiltinSkills
      .mockResolvedValueOnce({ uninstalled: ['five-whys', 'explain-for'] })
      .mockResolvedValue({ uninstalled: ['explain-for'] })
    render(<Harness />)
    expect(await screen.findByText('five-whys')).toBeInTheDocument()
    expect(screen.getByText('explain-for')).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole('button', { name: 'Restore' })[0])

    await waitFor(() => expect(mockApi.restoreBuiltinSkill).toHaveBeenCalledWith('five-whys'))
    await waitFor(() => expect(screen.queryByText('five-whys')).not.toBeInTheDocument())
    expect(screen.getByText('explain-for')).toBeInTheDocument()
  })

  it('reports a failed list request instead of rendering nothing', async () => {
    mockApi.uninstalledBuiltinSkills.mockRejectedValue(new Error('500'))
    render(<Harness />)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load the uninstalled built-in skills',
    )
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument()
  })

  it('keeps the row and reports a failed restore', async () => {
    mockApi.uninstalledBuiltinSkills.mockResolvedValue({ uninstalled: ['five-whys'] })
    mockApi.restoreBuiltinSkill.mockRejectedValue(new Error('boom'))
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not restore this skill')
    expect(screen.getByText('five-whys')).toBeInTheDocument()
  })

  it('withholds the agent hand-off on both notices while an edit is latched', async () => {
    mockApi.uninstalledBuiltinSkills.mockRejectedValueOnce(new Error('500'))
    const { unmount } = render(<Harness askAgent={false} />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('Ask the agent')).not.toBeInTheDocument()
    unmount()

    mockApi.uninstalledBuiltinSkills.mockResolvedValue({ uninstalled: ['five-whys'] })
    mockApi.restoreBuiltinSkill.mockRejectedValue(new Error('boom'))
    render(<Harness askAgent={false} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not restore this skill')
    expect(screen.queryByText('Ask the agent')).not.toBeInTheDocument()
  })

  it('offers the agent hand-off by default', async () => {
    mockApi.uninstalledBuiltinSkills.mockRejectedValue(new Error('500'))
    render(<Harness />)
    expect(await screen.findByText('Ask the agent')).toBeInTheDocument()
  })
})
