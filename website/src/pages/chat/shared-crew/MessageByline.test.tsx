import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../../api/sharedCrewMode'
import { MessageByline } from './MessageByline'
import { useSharedCrewMode } from '../../../hooks/sharedCrew/useSharedCrew'

vi.mock('../../../api/client', () => ({ api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn() } }))
const viewer = vi.mocked(api.sharedCrewViewer)

const person = (id: string, name: string, kind: 'human' | 'unknown' = 'human') => ({ id, kind, display: { name, login: '', captured_at: null } })
const authored = (id: string, name: string, kind: 'human' | 'unknown' = 'human') => ({ author: person(id, name, kind) })

function storeWith(slots: Array<Record<string, unknown>>) {
  const state = { dashboard: { slots } }
  return { getState: () => state, subscribe: () => () => {}, dispatch: (a: unknown) => a }
}

function mount(node: ReactNode, slots: Array<Record<string, unknown>> | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = <QueryClientProvider client={qc}>{node}</QueryClientProvider>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return render(slots ? <Provider store={storeWith(slots) as any}>{tree}</Provider> : tree)
}

const bylines = () => screen.queryAllByTestId('message-byline').map(el => el.textContent)

const SLOT = { key: 's1', attribution: { creator: person('p_ann', 'Ann'), availability: 'recorded', created_at: 1 } }

beforeEach(() => {
  vi.clearAllMocks()
  viewer.mockResolvedValue(person('p_bob', 'Bob'))
  // A team (network) Crew: the mode in which bylines render at all.
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [] })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({
    schema_version: 1, mode: 'network', crew_id: 'crew-a', viewer: person('p_bob', 'Bob'),
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
  } satisfies CrewModeResponse)
})

describe('MessageByline', () => {
  it('shows no label on a message from the session creator', async () => {
    // A sibling message from another participant in the same session is the
    // witness: its byline renders only once the mode and owner are known, so the
    // creator's missing label is then a decision, not a not-yet-loaded state.
    mount(<><MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(viewer).toHaveBeenCalled())
    await screen.findByTestId('message-byline')
    expect(bylines()).toEqual(['Cy'])
  })

  it('names a message from any other participant, the viewer included', async () => {
    mount(<MessageByline meta={authored('p_bob', 'Bob')} slotKey="s1" />, [SLOT])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Bob')
  })

  it('labels an author with no recorded person as Unattributed', async () => {
    mount(<MessageByline meta={authored('unknown', '', 'unknown')} slotKey="s1" />, [SLOT])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Unattributed')
  })

  it('treats the viewer as owner in a session with no recorded creator', async () => {
    // Cy's byline needs the viewer as owner, so once it renders the viewer's own
    // message has been judged against a known owner.
    mount(<><MessageByline meta={authored('p_bob', 'Bob')} slotKey="s2" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s2" /></>, [{ key: 's2', attribution: null }])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Cy')
    expect(bylines()).toEqual(['Cy'])
  })

  it('renders without a store and renders nothing for an unstamped message', async () => {
    mount(<MessageByline meta={authored('p_cy', 'Cy')} />, null)
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Cy')
    const { container } = mount(<MessageByline meta={{}} />, null)
    expect(container.innerHTML).toBe('')
  })
})

describe('MessageByline for a Slack sender', () => {
  const slackAuthor = (via: string = 'slack') => ({ author: { ...person('p_alice', 'Alice Example'), via } })

  it('names the Slack person with the Slack mark beside the name', async () => {
    mount(<MessageByline meta={slackAuthor()} slotKey="s1" />, [SLOT])
    const line = await screen.findByTestId('message-byline')
    expect(line.getAttribute('title')).toBe('Sent by Alice Example via Slack')
    expect(line.querySelector('.truncate')?.textContent).toBe('Alice Example')
    const mark = screen.getByTestId('message-byline-via')
    expect(mark.getAttribute('data-via')).toBe('slack')
    expect(mark.querySelector('img')).not.toBeNull()
    expect(mark.textContent).toBe('via Slack')
  })

  it('labels a Slack person with no cached name as a Slack user, never Unattributed', async () => {
    mount(<MessageByline meta={{ author: { ...person('p_anon', ''), via: 'slack' } }} slotKey="s1" />, [SLOT])
    const line = await screen.findByTestId('message-byline')
    expect(line.querySelector('.truncate')?.textContent).toBe('Slack user')
  })

  it('draws no mark for a channel this build does not know', async () => {
    mount(<MessageByline meta={slackAuthor('carrier-pigeon')} slotKey="s1" />, [SLOT])
    const line = await screen.findByTestId('message-byline')
    expect(line.textContent).toBe('Alice Example')
    expect(screen.queryByTestId('message-byline-via')).toBeNull()
  })

  it('shows no Slack byline on a personal Crew, like every other byline', async () => {
    vi.mocked(api.sharedCrewMode).mockResolvedValue({
      schema_version: 1, mode: 'personal', crew_id: 'crew-a', viewer: person('p_bob', 'Bob'),
      team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
      console: { state: 'not_offered', url: '' }, adapters: {}, systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
    } satisfies CrewModeResponse)
    // The witness reads the same mode query the byline gates on, so once it
    // renders the byline has rendered against the loaded personal mode too:
    // its absence is then the decision, not a not-yet-loaded state.
    function ModeLoaded() {
      return useSharedCrewMode().isSuccess ? <span data-testid="mode-loaded" /> : null
    }
    mount(<><MessageByline meta={slackAuthor()} slotKey="s1" /><ModeLoaded /></>, [SLOT])
    await screen.findByTestId('mode-loaded')
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })
})
