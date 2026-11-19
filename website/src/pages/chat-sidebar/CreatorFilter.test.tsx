import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { loadCreatorFilter, saveCreatorFilter } from '../../api/sharedCrewMode'
import { SharedCrewReadNotices, useSharedCrewListView } from './CreatorFilter'
import { SessionCreatorByline, SharedCrewBylineContext } from './SessionCreatorByline'

vi.mock('../../api/client', () => ({
  api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn() },
}))
const people = vi.mocked(api.sharedCrewPeople)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)

const human = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: `${name.toLowerCase()}@example.test`, captured_at: null } })
const UNKNOWN = { id: 'p_unknown', kind: 'unknown' as const, display: { name: '', login: '', captured_at: null } }
const attribution = (name: string, id = 'p_b') => ({ creator: human(id, name), availability: 'recorded' as const, created_at: 1 })

// The list view reads capabilities first; only then are the mode and People
// queries enabled. Waits that span that chain get this ceiling.
const CHAIN_TIMEOUT_MS = 5000

function modeBody(mode: CrewModeResponse['mode'], crewId = 'crew-a', viewerId = 'p_me'): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: crewId, viewer: human(viewerId, 'Me'),
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
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
  // The Shared Crew preview on, and a teammate who joined: the state in which
  // the filter and bylines show at all.
  caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: human('p_tm', 'Tam'), sources: ['observed' as const], is_viewer: false }] })
  modeApi.mockResolvedValue(modeBody('network'))
})

describe('SharedCrewReadNotices', () => {
  it('a failed People read shows the error notice at the top of the list', async () => {
    people.mockRejectedValue(new Error('people read failed'))
    modeApi.mockResolvedValue(modeBody('network'))
    function Probe() {
      useSharedCrewListView()
      return <SharedCrewReadNotices />
    }
    mount(<Probe />)
    expect(await screen.findByTestId('creator-filter-people-error', {}, { timeout: CHAIN_TIMEOUT_MS })).toHaveTextContent('people read failed')
  })
})

describe('useSharedCrewListView', () => {
  it('hides the filter and bylines on a personal Crew', async () => {
    modeApi.mockResolvedValue(modeBody('personal'))
    const { result } = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(modeApi).toHaveBeenCalled())
    await waitFor(() => expect(result.current.bylines.hidden).toBe(true))
    expect(result.current.creatorFilterShown).toBe(false)
    expect(result.current.noticesAvailable).toBe(false)
    expect(result.current.creatorKeys.size).toBe(0)
  })

  it('hides the filter and bylines with the Shared Crew preview off, reading no mode or People', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    window.localStorage.setItem('kc.shared-crew.creator-filter.crew-a', 'mine')
    const { result } = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(caps).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.bylines.hidden).toBe(true)
    expect(result.current.creatorFilterShown).toBe(false)
    expect(result.current.creatorKeys.size).toBe(0)
    expect(modeApi).not.toHaveBeenCalled()
    expect(people).not.toHaveBeenCalled()
  })

  it.each([
    ['no one besides the owner joined', []],
    // A Slack sender is a session creator only: it never connected to this Crew.
    ['the only other person is a Slack sender', [{ principal: human('p_slack', 'U0BP2QZ7NJ2'), sources: ['session_creator' as const], is_viewer: false }]],
    ['the only invite is not used yet', [{ principal: human('p_inv', 'Ivy'), sources: ['tailscale_share' as const], is_viewer: false }]],
  ])('hides the filter and bylines on a network Crew when %s', async (_why, rows) => {
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [
      { principal: human('p_me', 'Me'), sources: ['observed'], is_viewer: true, is_owner: true },
      ...rows,
    ] })
    const { result } = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(people).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(modeApi).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => {})
    expect(result.current.bylines.hidden).toBe(true)
    expect(result.current.creatorFilterShown).toBe(false)
  })

  it('shows the filter and bylines once a teammate joins', async () => {
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [
      { principal: human('p_me', 'Me'), sources: ['observed'], is_viewer: true, is_owner: true },
      { principal: human('p_tm', 'Tam'), sources: ['observed'], is_viewer: false },
    ] })
    const { result } = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.bylines.hidden).toBe(false), { timeout: CHAIN_TIMEOUT_MS })
    expect(result.current.creatorFilterShown).toBe(true)
  })

  it('never guesses a mode it could not read: no bylines, no filter, the notice shows the error', async () => {
    // A team path is set up and another person is seen, so a guessed mode would be network.
    caps.mockResolvedValue({
      ...ALL_UNAVAILABLE,
      attribution: 'available',
      shared_crew_preview: true,
      setup_paths: {
        tailscale: { state: 'available', reason: '' },
        kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
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
          <span data-testid="probe">{`${view.bylines.hidden}|${view.creatorKeys.size}|${view.creatorFilterShown}|${view.noticesAvailable}`}</span>
          {view.noticesAvailable && <SharedCrewReadNotices />}
        </>
      )
    }
    render(<W><Probe /></W>)
    await waitFor(() => expect(screen.getByTestId('creator-filter-mode-error')).toBeInTheDocument())
    expect(screen.getByTestId('probe').textContent).toBe('true|0|false|true')
    expect(screen.getByTestId('creator-filter-mode-error').textContent).toContain('mode route missing')
    // Showing the error does not re-ask the route.
    expect(modeApi).toHaveBeenCalledTimes(1)
  })

  it('remembers the selection per Crew and labels the viewer', async () => {
    modeApi.mockResolvedValue(modeBody('network', 'crew-a'))
    const first = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(first.result.current.creatorFilterShown).toBe(true))
    expect(first.result.current.bylines).toEqual({ hidden: false, viewerId: 'p_me' })
    act(() => first.result.current.toggleCreator('unattributed'))
    act(() => first.result.current.toggleCreator('p_tm'))
    expect([...first.result.current.creatorKeys]).toEqual(['unattributed', 'p_tm'])
    act(() => first.result.current.toggleCreator('p_tm'))
    expect([...first.result.current.creatorKeys]).toEqual(['unattributed'])
    first.unmount()

    // Same Crew again: the choice comes back.
    const again = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect([...again.result.current.creatorKeys]).toEqual(['unattributed']))
    act(() => again.result.current.clearCreators())
    expect(again.result.current.creatorKeys.size).toBe(0)
    expect(window.localStorage.getItem('kc.shared-crew.creator-filter.crew-a')).toBeNull()
    again.unmount()

    // Another Crew keeps its own (default) choice.
    window.localStorage.setItem('kc.shared-crew.creator-filter.crew-a', '["mine"]')
    modeApi.mockResolvedValue(modeBody('network', 'crew-b'))
    const other = renderHook(() => useSharedCrewListView(), { wrapper: wrapper() })
    await waitFor(() => expect(other.result.current.creatorFilterShown).toBe(true))
    expect(other.result.current.creatorKeys.size).toBe(0)
  })

  it('reads the older one-choice value as a one-key selection', () => {
    const store = window.localStorage
    store.setItem('kc.shared-crew.creator-filter.c', 'unknown')
    expect(loadCreatorFilter('c')).toEqual(['unattributed'])
    store.setItem('kc.shared-crew.creator-filter.c', 'p_bob')
    expect(loadCreatorFilter('c')).toEqual(['p_bob'])
    store.setItem('kc.shared-crew.creator-filter.c', 'all')
    expect(loadCreatorFilter('c')).toEqual([])
    store.setItem('kc.shared-crew.creator-filter.c', '["mine","p_b","not a key","mine"]')
    expect(loadCreatorFilter('c')).toEqual(['mine', 'p_b'])
    store.setItem('kc.shared-crew.creator-filter.c', '[broken')
    expect(loadCreatorFilter('c')).toEqual([])
    saveCreatorFilter('c', ['p_b', 'mine'])
    expect(store.getItem('kc.shared-crew.creator-filter.c')).toBe('["p_b","mine"]')
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

  it('names the owner on a legacy session, and keeps Unattributed for a creator nobody can name', () => {
    const owner = { id: 'p_owner', kind: 'human' as const, display: { name: "Bolin's MacBook", login: '', captured_at: null } }
    const legacy = { creator: owner, availability: 'legacy_missing' as const, created_at: null }
    const { rerender } = render(<SessionCreatorByline attribution={legacy} />)
    expect(screen.getByTitle("Created by Bolin's MacBook")).toBeInTheDocument()
    expect(screen.queryByTitle(/Unattributed/)).toBeNull()
    // A gateway that could not read this machine's name still sends the token subject.
    rerender(<SessionCreatorByline attribution={{ ...legacy, creator: { ...owner, display: { name: 'local-app', login: '', captured_at: null } } }} />)
    expect(screen.getByTitle('Created by Crew owner')).toBeInTheDocument()
    rerender(<SessionCreatorByline attribution={{ ...legacy, creator: UNKNOWN }} />)
    expect(screen.getByTitle('Created by Unattributed')).toBeInTheDocument()
  })

  it('reads the owner as You to the owner viewing a legacy session', () => {
    const owner = { id: 'p_owner', kind: 'human' as const, display: { name: "Bolin's MacBook", login: '', captured_at: null } }
    mount(<SessionCreatorByline attribution={{ creator: owner, availability: 'legacy_missing', created_at: null }} />, 'p_owner')
    expect(screen.getByTitle('Created by You')).toBeInTheDocument()
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
