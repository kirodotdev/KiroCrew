import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { MessageByline, MessageSenderName, OtherSenderBubble, SpeakerHeading, useUserRowSpeaker } from './MessageByline'

vi.mock('../../../api/client', () => ({ api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn(), listApps: vi.fn(async () => []) } }))
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

/** The byline reads capabilities first; only then are the mode and People
 *  queries enabled, and the viewer read sits beside them. Waits that span
 *  that chain get this ceiling instead of the 1000ms default. */
const CHAIN_TIMEOUT_MS = 5000

const bylines = () => screen.queryAllByTestId('message-byline').map(el => el.textContent)

/** What `OtherSenderBubble` hands its children, as one word: someone else's
 *  (`other`), nobody's yet (`held`), or the viewer's `own`. */
const drawnAs = (other: boolean, held: boolean) => (other ? 'other' : held ? 'held' : 'own')

const SLOT = { key: 's1', attribution: { creator: person('p_ann', 'Ann'), availability: 'recorded', created_at: 1 } }

beforeEach(() => {
  vi.clearAllMocks()
  viewer.mockResolvedValue(person('p_bob', 'Bob'))
  // A team (network) Crew with the Shared Crew preview on and a teammate who
  // joined: the state in which bylines render at all.
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: person('p_tm', 'Tam'), sources: ['observed' as const], is_viewer: false }] })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({
    schema_version: 1, mode: 'network', crew_id: 'crew-a', viewer: person('p_bob', 'Bob'),
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
  } satisfies CrewModeResponse)
})

describe('MessageByline', () => {
  it('names the session creator to a viewer who did not create the session', async () => {
    mount(<><MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(bylines()).toEqual(['Ann', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('shows no label on the viewer\'s own message', async () => {
    // A sibling message from another participant is the witness: its byline
    // renders only once the mode and viewer are known, so the viewer's missing
    // label is then a decision, not a not-yet-loaded state.
    mount(<><MessageByline meta={authored('p_bob', 'Bob')} slotKey="s1" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(viewer).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await screen.findByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(bylines()).toEqual(['Cy'])
  })

  it('the creator reading their own session sees only the others named', async () => {
    viewer.mockResolvedValue(person('p_ann', 'Ann'))
    mount(<><MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" /><MessageByline meta={authored('p_bob', 'Bob')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(viewer).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await screen.findByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(bylines()).toEqual(['Bob'])
  })

  it('names everyone, the session creator included, until the viewer read lands', async () => {
    // A teammate opening the creator's session: the creator's messages must not
    // be drawn as the teammate's own while nobody said who the teammate is.
    viewer.mockReturnValue(new Promise(() => {}))
    mount(<><MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(bylines()).toEqual(['Ann', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
  })

  it.each([
    ['fails', () => viewer.mockRejectedValue(new Error('offline'))],
    ['reports nobody', () => viewer.mockResolvedValue(null)],
    ['reports an Unknown principal', () => viewer.mockResolvedValue(person('unknown', '', 'unknown'))],
  ])('names everyone when the viewer read %s', async (_case, arrange) => {
    arrange()
    mount(<><MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" /><MessageByline meta={authored('p_bob', 'Bob')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(bylines()).toEqual(['Ann', 'Bob']), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('draws a stamped message as someone else\'s until the viewer is known, then the viewer\'s own as own', async () => {
    let land: (p: ReturnType<typeof person>) => void = () => {}
    viewer.mockReturnValue(new Promise(resolve => { land = resolve }))
    const tone = (id: string) => screen.getByTestId('tone-' + id).textContent
    mount(<>
      <OtherSenderBubble meta={authored('p_ann', 'Ann')} slotKey="s1">{(other, held) => <span data-testid="tone-ann">{drawnAs(other, held)}</span>}</OtherSenderBubble>
      <OtherSenderBubble meta={authored('p_bob', 'Bob')} slotKey="s1">{(other, held) => <span data-testid="tone-bob">{drawnAs(other, held)}</span>}</OtherSenderBubble>
    </>, [SLOT])
    await waitFor(() => expect(tone('ann')).toBe('other'), { timeout: CHAIN_TIMEOUT_MS })
    expect(tone('bob')).toBe('other')
    land(person('p_bob', 'Bob'))
    await waitFor(() => expect(tone('bob')).toBe('own'), { timeout: CHAIN_TIMEOUT_MS })
    expect(tone('ann')).toBe('other')
  })

  it('a personal Crew draws every message as own, whoever the viewer read names', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue(ALL_UNAVAILABLE)
    viewer.mockReturnValue(new Promise(() => {}))
    mount(<OtherSenderBubble meta={authored('p_ann', 'Ann')} slotKey="s1">{(other, held) => <span data-testid="tone">{drawnAs(other, held)}</span>}</OtherSenderBubble>, [SLOT])
    await waitFor(() => expect(screen.getByTestId('tone').textContent).toBe('own'), { timeout: CHAIN_TIMEOUT_MS })
    expect(bylines()).toEqual([])
  })

  it.each([
    ['capabilities', () => vi.mocked(api.sharedCrewCapabilities).mockReturnValue(new Promise(() => {}))],
    ['the mode', () => vi.mocked(api.sharedCrewMode).mockReturnValue(new Promise(() => {}))],
    ['people', () => vi.mocked(api.sharedCrewPeople).mockReturnValue(new Promise(() => {}))],
    ['a failed mode read', () => vi.mocked(api.sharedCrewMode).mockRejectedValue(new Error('offline'))],
  ])('while attribution rests on %s not loaded, only the known viewer\'s message is drawn as own', async (_case, arrange) => {
    arrange()
    const tone = (id: string) => screen.getByTestId('tone-' + id).textContent
    mount(<>
      <OtherSenderBubble meta={authored('p_ann', 'Ann')}>{(other, held) => <span data-testid="tone-ann">{drawnAs(other, held)}</span>}</OtherSenderBubble>
      <OtherSenderBubble meta={authored('p_bob', 'Bob')}>{(other, held) => <span data-testid="tone-bob">{drawnAs(other, held)}</span>}</OtherSenderBubble>
    </>, null)
    // Before any read answers a stamped message is held: nobody's yet.
    expect(tone('ann')).toBe('held')
    expect(tone('bob')).toBe('held')
    await waitFor(() => expect(tone('ann')).toBe('other'), { timeout: CHAIN_TIMEOUT_MS })
    expect(tone('bob')).toBe('own')
  })

  it('labels an author with no recorded person as Unattributed', async () => {
    mount(<MessageByline meta={authored('unknown', '', 'unknown')} slotKey="s1" />, [SLOT])
    expect((await screen.findByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })).textContent).toBe('Unattributed')
  })

  it('leaves the viewer\'s own message unlabelled in a session with no recorded creator', async () => {
    // Cy's byline needs the viewer to be known, so once it renders the viewer's
    // own message has been judged against the known viewer.
    mount(<><MessageByline meta={authored('p_bob', 'Bob')} slotKey="s2" /><MessageByline meta={authored('p_cy', 'Cy')} slotKey="s2" /></>, [{ key: 's2', attribution: null }])
    expect((await screen.findByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })).textContent).toBe('Cy')
    expect(bylines()).toEqual(['Cy'])
  })

  it('renders without a store and renders nothing for an unstamped message', async () => {
    mount(<MessageByline meta={authored('p_cy', 'Cy')} />, null)
    expect((await screen.findByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })).textContent).toBe('Cy')
    const { container } = mount(<MessageByline meta={{}} />, null)
    expect(container.innerHTML).toBe('')
  })
})

describe('SpeakerHeading', () => {
  const headings = () => screen.queryAllByRole('heading', { level: 2 }).map(el => el.textContent)

  it('names a teammate and says You for the viewer, the same rule as the byline', async () => {
    mount(<><SpeakerHeading meta={authored('p_bob', 'Bob')} slotKey="s1" /><SpeakerHeading meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(headings()).toEqual(['You', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('names the session creator, never You, while the viewer read is pending', async () => {
    viewer.mockReturnValue(new Promise(() => {}))
    mount(<><SpeakerHeading meta={authored('p_ann', 'Ann')} slotKey="s1" /><SpeakerHeading meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(headings()).toEqual(['Ann', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('says You on a message with no author stamp', () => {
    mount(<SpeakerHeading meta={{}} />, null)
    expect(headings()).toEqual(['You'])
  })

  it('names the app on a row an app sent', () => {
    mount(<SpeakerHeading meta={{ turnActor: 'app', appLabel: 'notes-app' }} />, null)
    expect(headings()).toEqual(['notes-app'])
  })

  it('names the sending session on a relayed message, never You', () => {
    mount(<SpeakerHeading meta={{}} relayedFrom="Research session" />, null)
    expect(headings()).toEqual(['Research session'])
  })
})

describe('useUserRowSpeaker', () => {
  // The text the live region speaks when the row is not drawn, so it must say
  // what SpeakerHeading would: the same mount, the same reads.
  function Spoken({ meta, slotKey, content, sessions }: {
    meta?: Record<string, unknown>
    slotKey?: string
    content?: string
    sessions?: ReadonlyMap<string, string>
  }) {
    const resolved = useUserRowSpeaker(meta, slotKey, content, sessions)
    return <span data-testid="spoken" data-own={String(resolved.own)}>{resolved.speaker}</span>
  }
  const spoken = () => screen.getAllByTestId('spoken').map(el => el.textContent)
  const owned = () => screen.getAllByTestId('spoken').map(el => el.getAttribute('data-own'))

  it('names a teammate and says You for the viewer, as the heading does', async () => {
    mount(<><Spoken meta={authored('p_bob', 'Bob')} slotKey="s1" /><Spoken meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(spoken()).toEqual(['You', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
    expect(owned()).toEqual(['true', 'false'])
  })

  it('names the session creator, never You, while the viewer read is pending', async () => {
    viewer.mockReturnValue(new Promise(() => {}))
    mount(<><Spoken meta={authored('p_ann', 'Ann')} slotKey="s1" /><Spoken meta={authored('p_cy', 'Cy')} slotKey="s1" /></>, [SLOT])
    await waitFor(() => expect(spoken()).toEqual(['Ann', 'Cy']), { timeout: CHAIN_TIMEOUT_MS })
    expect(owned()).toEqual(['false', 'false'])
  })

  it('says You on a message with no author stamp', () => {
    mount(<Spoken meta={{}} />, null)
    expect(spoken()).toEqual(['You'])
    expect(owned()).toEqual(['true'])
  })

  it('names the app on a row an app sent', () => {
    mount(<Spoken meta={{ turnActor: 'app', appLabel: 'notes-app' }} />, null)
    expect(spoken()).toEqual(['notes-app'])
    expect(owned()).toEqual(['false'])
  })

  it('uses the linked session title before the generic sender role', () => {
    const relay = { relay: { senderKey: 'dashboard:other', via: 'session_send' } }
    const sessions = new Map([['dashboard:other', 'Research session']])
    mount(<Spoken meta={relay} sessions={sessions} />, null)
    expect(spoken()).toEqual(['Research session'])
    expect(owned()).toEqual(['false'])
  })
})

describe('a stamped message while neither the Crew mode nor the viewer is known', () => {
  // One row as every surface reads it: the bubble, the visible byline, the
  // offscreen heading and the live-region speaker with its own-send flag.
  function Row({ id, name }: { id: string; name: string }) {
    const meta = authored(id, name)
    const said = useUserRowSpeaker(meta)
    return (
      <div data-testid={'row-' + id}>
        <OtherSenderBubble meta={meta}>{(other, held) => <span data-testid="tone">{drawnAs(other, held)}</span>}</OtherSenderBubble>
        <MessageByline meta={meta} />
        <SpeakerHeading meta={meta} />
        <span data-testid="said" data-own={String(said.own)}>{said.speaker}</span>
        <span data-testid="find-label"><MessageSenderName meta={meta} fallback="You" /></span>
      </div>
    )
  }
  /** [bubble, visible name, heading, live-region speaker, own send] of a row.
   *  The find panel's label for the row is checked against the heading here:
   *  the two are one rule. */
  function row(id: string) {
    const el = within(screen.getByTestId('row-' + id))
    const said = el.getByTestId('said')
    expect(el.getByTestId('find-label').textContent).toBe(el.getByRole('heading', { level: 2 }).textContent)
    return [
      el.getByTestId('tone').textContent,
      el.queryByTestId('message-byline')?.textContent ?? '',
      el.getByRole('heading', { level: 2 }).textContent,
      said.textContent,
      said.getAttribute('data-own'),
    ]
  }
  const rows = () => [row('p_ann'), row('p_bob')]
  // Ann is a teammate and Bob is the viewer (`beforeEach`); neither is known
  // to the page until a read answers.
  const mountRows = () => mount(<><Row id="p_ann" name="Ann" /><Row id="p_bob" name="Bob" /></>, null)
  const HELD_ANN = ['held', '', 'Ann', 'Ann', 'false']
  const HELD_BOB = ['held', '', 'Bob', 'Bob', 'false']
  const pending = () => new Promise<never>(() => {})

  it('is drawn as nobody\'s: not own, not a teammate\'s, unnamed, and never spoken as You', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockReturnValue(pending())
    viewer.mockReturnValue(pending())
    mountRows()
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
    // Still held once both reads are out and unanswered.
    await waitFor(() => expect(viewer).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(api.sharedCrewCapabilities).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
  })

  it('the viewer read answering alone draws a teammate apart, unnamed, and the viewer\'s own as own', async () => {
    let land: (p: ReturnType<typeof person>) => void = () => {}
    vi.mocked(api.sharedCrewCapabilities).mockReturnValue(pending())
    viewer.mockReturnValue(new Promise(resolve => { land = resolve }))
    mountRows()
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
    land(person('p_bob', 'Bob'))
    await waitFor(() => expect(rows()).toEqual([
      ['other', '', 'Ann', 'Ann', 'false'],
      ['own', '', 'You', 'You', 'true'],
    ]), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('the mode answering alone on a Crew that attributes names every stamped message', async () => {
    // Capabilities, then the mode and People reads they enable; the viewer
    // read never answers.
    viewer.mockReturnValue(pending())
    mountRows()
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
    await waitFor(() => expect(rows()).toEqual([
      ['other', 'Ann', 'Ann', 'Ann', 'false'],
      ['other', 'Bob', 'Bob', 'Bob', 'false'],
    ]), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('the mode answering alone on a personal Crew draws every stamped message as own', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue(ALL_UNAVAILABLE)
    viewer.mockReturnValue(pending())
    mountRows()
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
    await waitFor(() => expect(rows()).toEqual([
      ['own', '', 'You', 'You', 'true'],
      ['own', '', 'You', 'You', 'true'],
    ]), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('both reads answering names the teammate apart and leaves the viewer\'s own as own', async () => {
    // Capabilities, then the mode and People reads, beside the viewer read.
    mountRows()
    await waitFor(() => expect(rows()).toEqual([
      ['other', 'Ann', 'Ann', 'Ann', 'false'],
      ['own', '', 'You', 'You', 'true'],
    ]), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('both reads failing leaves it held, and nothing is drawn as the viewer\'s own', async () => {
    // The two reads the rule rests on, in the rows' own query cache: the
    // marker shows only once both have failed, so the rows are read after the
    // failures, not before them.
    function ReadsFailed() {
      const capabilities = useSharedCrewCapabilities()
      const who = useSharedCrewViewer()
      return capabilities.isError && who.isError ? <span data-testid="reads-failed" /> : null
    }
    vi.mocked(api.sharedCrewCapabilities).mockRejectedValue(new Error('offline'))
    viewer.mockRejectedValue(new Error('offline'))
    mount(<><Row id="p_ann" name="Ann" /><Row id="p_bob" name="Bob" /><ReadsFailed /></>, null)
    await screen.findByTestId('reads-failed', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(rows()).toEqual([HELD_ANN, HELD_BOB])
  })
})
