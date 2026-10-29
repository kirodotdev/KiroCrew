import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { CreatorFilterBar, CreatorFilteredSessions, useSharedCrewListView } from './CreatorFilter'
import { SessionCreatorByline, SharedCrewBylineContext } from './SessionCreatorByline'

vi.mock('../../api/client', () => ({
  api: { sharedCrewSessions: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn() },
}))
const sessions = vi.mocked(api.sharedCrewSessions)
const people = vi.mocked(api.sharedCrewPeople)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)

const human = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: `${name.toLowerCase()}@example.test`, captured_at: null } })
const UNKNOWN = { id: 'p_unknown', kind: 'unknown' as const, display: { name: '', login: '', captured_at: null } }
const attribution = (name: string, id = 'p_b') => ({ creator: human(id, name), availability: 'recorded' as const, created_at: 1 })

function modeBody(mode: CrewModeResponse['mode'], crewId = 'crew-a', viewerId = 'p_me'): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: crewId, viewer: human(viewerId, 'Me'),
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
  }
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function mount(ui: React.ReactElement, viewerId = '') {
  const W = wrapper()
  return render(<W><SharedCrewBylineContext.Provider value={{ hidden: false, viewerId }}>{ui}</SharedCrewBylineContext.Provider></W>)
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
  people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [] })
  modeApi.mockResolvedValue(modeBody('network'))
})

describe('CreatorFilteredSessions', () => {
  it('asks the server to filter and page, and shows the server counts', async () => {
    sessions.mockResolvedValueOnce({ schema_version: 1, items: [{ key: 'k1', title: 'One', attribution: attribution('Bob') }], total_matching: 120, hidden_by_filter: 30, next_cursor: 'c2' })
    sessions.mockResolvedValueOnce({ schema_version: 1, items: [{ key: 'k2', title: 'Two' }], total_matching: 120, hidden_by_filter: 30, next_cursor: '' })
    const onOpen = vi.fn()
    mount(<CreatorFilteredSessions creator="mine" q=" deploy " connected onOpen={onOpen} onClear={() => {}} />)
    expect(await screen.findByText('One')).toBeInTheDocument()
    expect(sessions).toHaveBeenLastCalledWith({ creator: 'mine', q: 'deploy', cursor: undefined, limit: 50 })
    expect(screen.getByTestId('creator-filter-counts').textContent).toMatch(/120 matching · 30 hidden by this filter/)
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    expect(await screen.findByText('Two')).toBeInTheDocument()
    expect(sessions).toHaveBeenLastCalledWith({ creator: 'mine', q: 'deploy', cursor: 'c2', limit: 50 })
    // The second page renders what the server sent: no client-side filtering.
    expect(screen.getByText(/Unattributed/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Two'))
    expect(onOpen).toHaveBeenCalledWith('k2', 'Two')
  })

  it('re-reads a cached creator page when returning to the filter', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const page = { schema_version: 1, items: [{ key: 'k1', title: 'One' }], total_matching: 1, hidden_by_filter: 0, next_cursor: '' }
    sessions.mockResolvedValue(page)
    const tree = (show: boolean) => (
      <QueryClientProvider client={qc}>
        {show && <CreatorFilteredSessions creator="mine" q="" connected onOpen={() => {}} onClear={() => {}} />}
      </QueryClientProvider>
    )
    const { rerender } = render(tree(true))
    expect(await screen.findByText('One')).toBeInTheDocument()
    rerender(tree(false))
    sessions.mockResolvedValue({ ...page, items: [{ key: 'k2', title: 'Two' }] })
    rerender(tree(true))
    expect(await screen.findByText('Two')).toBeInTheDocument()
    expect(sessions).toHaveBeenCalledTimes(2)
  })

  it('sends the older unknown spelling as unattributed, and degrades when the route is not served', async () => {
    sessions.mockResolvedValue(null)
    const onClear = vi.fn()
    mount(<CreatorFilteredSessions creator="unknown" q="" connected onOpen={() => {}} onClear={onClear} />)
    expect(await screen.findByTestId('creator-filter-unavailable')).toBeInTheDocument()
    expect(sessions).toHaveBeenLastCalledWith({ creator: 'unattributed', q: undefined, cursor: undefined, limit: 50 })
    fireEvent.click(screen.getByRole('button', { name: 'Show all sessions' }))
    expect(onClear).toHaveBeenCalled()
  })

  it('labels the verified viewer You and unattended work by its trigger', async () => {
    sessions.mockResolvedValueOnce({
      schema_version: 1, total_matching: 2, hidden_by_filter: 0, next_cursor: '',
      items: [
        { key: 'mine', title: 'Mine', attribution: attribution('Me', 'p_me') },
        { key: 'cron', title: 'Nightly', attribution: { creator: UNKNOWN, availability: 'legacy_missing', created_at: null, trigger: { kind: 'scheduled', ref: 'abc12345', name: 'Nightly digest', configured_by: null } } },
      ],
    })
    mount(<CreatorFilteredSessions creator="all" q="" connected onOpen={() => {}} onClear={() => {}} />, 'p_me')
    expect(await screen.findByText('Mine')).toBeInTheDocument()
    expect(screen.getByTestId('creator-filter-row-mine').textContent).toContain('You')
    expect(screen.getByTestId('creator-filter-row-cron').textContent).toContain('Scheduled: Nightly digest')
    expect(screen.getByTestId('creator-filter-row-cron').textContent).not.toContain('Unattributed')
  })

  it('shows an error through the shared notice', async () => {
    sessions.mockRejectedValue(new Error('boom'))
    mount(<CreatorFilteredSessions creator="all" q="" connected onOpen={() => {}} onClear={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('creator-filter-error')).toBeInTheDocument())
  })
})

describe('CreatorFilterBar', () => {
  it('offers All, Mine, the people seen and Unattributed', async () => {
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [
      { principal: human('p_b', 'Bob'), sources: ['observed'], is_viewer: false },
      { principal: human('p_me', 'Me'), sources: ['observed'], is_viewer: true },
    ] })
    // The older `unknown` value shows as the Unattributed choice.
    mount(<CreatorFilterBar value="unknown" onChange={() => {}} />)
    const root = await screen.findByTestId('creator-filter')
    await waitFor(() => expect(root.textContent).toContain('Unattributed'))
    expect(root.textContent).not.toContain('Unknown')
  })

  it('a failed People read shows the error notice beside the choices', async () => {
    people.mockRejectedValue(new Error('people read failed'))
    modeApi.mockResolvedValue(modeBody('network'))
    mount(<CreatorFilterBar value="all" onChange={() => {}} />)
    expect(await screen.findByTestId('creator-filter-people-error')).toHaveTextContent('people read failed')
  })
})

describe('useSharedCrewListView', () => {
  it('hides the filter and bylines on a personal Crew', async () => {
    modeApi.mockResolvedValue(modeBody('personal'))
    const { result } = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(modeApi).toHaveBeenCalled())
    await waitFor(() => expect(result.current.bylines.hidden).toBe(true))
    expect(result.current.filterAvailable).toBe(false)
    expect(result.current.filter).toBe('all')
  })

  it('never guesses a mode it could not read: no bylines, no filter, the bar shows the error', async () => {
    // A team path is set up and another person is seen, so a guessed mode would be network.
    caps.mockResolvedValue({
      ...ALL_UNAVAILABLE,
      attribution: 'available',
      setup_paths: {
        tailscale: { state: 'available', reason: '' },
        kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
        systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
      },
    })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [
      { principal: human('p_b', 'Bob'), sources: ['observed'], is_viewer: false },
    ] })
    modeApi.mockRejectedValue(new Error('mode route missing'))
    window.localStorage.setItem('kc.shared-crew.creator-filter.crew-a', 'mine')
    const W = wrapper()
    function Probe() {
      const view = useSharedCrewListView()
      return (
        <>
          <span data-testid="probe">{`${view.bylines.hidden}|${view.filter}|${view.filterAvailable}`}</span>
          {view.filterAvailable && <CreatorFilterBar value={view.filter} onChange={view.setFilter} />}
        </>
      )
    }
    render(<W><Probe /></W>)
    await waitFor(() => expect(screen.getByTestId('creator-filter-mode-error')).toBeInTheDocument())
    expect(screen.getByTestId('probe').textContent).toBe('true|all|true')
    expect(screen.queryByTestId('creator-filter')).toBeNull()
    expect(screen.getByTestId('creator-filter-mode-error').textContent).toContain('mode route missing')
    // Showing the error does not re-ask the route.
    expect(modeApi).toHaveBeenCalledTimes(1)
  })

  it('remembers the selection per Crew and labels the viewer', async () => {
    modeApi.mockResolvedValue(modeBody('network', 'crew-a'))
    const first = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(first.result.current.filterAvailable).toBe(true))
    expect(first.result.current.bylines).toEqual({ hidden: false, viewerId: 'p_me' })
    act(() => first.result.current.setFilter('unknown'))
    expect(first.result.current.filter).toBe('unattributed')
    first.unmount()

    // Same Crew again: the choice comes back.
    const again = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(again.result.current.filter).toBe('unattributed'))
    again.unmount()

    // Another Crew keeps its own (default) choice.
    modeApi.mockResolvedValue(modeBody('network', 'crew-b'))
    const other = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(other.result.current.filterAvailable).toBe(true))
    expect(other.result.current.filter).toBe('all')
  })
})

describe('SessionCreatorByline', () => {
  it('renders inline, names the creator, and says Unattributed for a legacy session', () => {
    const { rerender } = render(<SessionCreatorByline attribution={attribution('Bob')} />)
    expect(screen.getByTestId('session-creator-byline').tagName).toBe('SPAN')
    expect(screen.getByTitle('Created by Bob (bob@example.test)')).toBeInTheDocument()
    rerender(<SessionCreatorByline attribution={{ creator: UNKNOWN, availability: 'legacy_missing', created_at: null }} />)
    expect(screen.getByTitle('Created by Unattributed')).toBeInTheDocument()
  })

  it('names the trigger of a scheduled session, never a person', () => {
    render(<SessionCreatorByline attribution={{ creator: UNKNOWN, availability: 'legacy_missing', created_at: null, trigger: { kind: 'scheduled', ref: 'abc12345', name: 'Nightly', configured_by: null } }} />)
    expect(screen.getByTitle('Started by Scheduled: Nightly')).toBeInTheDocument()
  })

  it('says You for the verified viewer and hides itself on a personal Crew', () => {
    const { rerender } = render(
      <SharedCrewBylineContext.Provider value={{ hidden: false, viewerId: 'p_b' }}><SessionCreatorByline attribution={attribution('Bob')} /></SharedCrewBylineContext.Provider>,
    )
    expect(screen.getByTestId('session-creator-byline').textContent).toContain('You')
    rerender(<SharedCrewBylineContext.Provider value={{ hidden: true, viewerId: 'p_b' }}><SessionCreatorByline attribution={attribution('Bob')} /></SharedCrewBylineContext.Provider>)
    expect(screen.queryByTestId('session-creator-byline')).toBeNull()
  })

  it('renders nothing without attribution', () => {
    const { container } = render(<SessionCreatorByline attribution={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
