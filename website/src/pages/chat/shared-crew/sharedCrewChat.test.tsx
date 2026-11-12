import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AcceptedWork, CapacityResponse, PresenceEntry, SharedDecision } from '../../../api/sharedCrew'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import UserMessage from '../UserMessage'
import { QueuedSendLabel, queuedChatTurn } from './QueuedSendLabel'
import { SessionCollabStrip } from './SessionCollabStrip'
import { PresenceChips, SessionPresence } from './PresenceChips'
import { refusalMessage, queuedReason } from './overloadCopy'
import { alreadyResolvedDecision, resolvedByLine, settledElsewhereLine } from '../../../components/chat-input/sharedDecision'
import { readSendReceipt } from '../../../utils/sendDelivery'

vi.mock('../../../api/client', () => ({
  api: {
    sharedCrewCancelWork: vi.fn(),
    sharedCrewCapabilities: vi.fn(),
    sharedCrewWork: vi.fn(),
    sharedCrewCapacity: vi.fn(),
    sharedCrewPresence: vi.fn(),
    sharedCrewDecisions: vi.fn(),
    sharedCrewSendState: vi.fn(),
    sharedCrewViewer: vi.fn(),
    sharedCrewTypingLease: vi.fn(),
    sharedCrewMode: vi.fn(),
    sharedCrewPeople: vi.fn(),
  },
}))

const cancel = vi.mocked(api.sharedCrewCancelWork)
const workRead = vi.mocked(api.sharedCrewWork)

const work = (over: Partial<AcceptedWork>): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state: 'queued', revision: 1,
  accepted_at: 1, expires_at: 2, deadline_at: null, reason: null, ...over,
})
const person = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const capacity = (scope: 'principal' | 'crew' | null): CapacityResponse => ({
  schema_version: 1,
  principal: { active: 2, queued: 1, active_limit: 2, queue_limit: 3 },
  crew: { active: 8, queued: 4, active_limit: 8, queue_limit: 4 },
  enforcement: 'hard', limiting_scope: scope, reason: null, retry_after_seconds: 5,
  queue_expiry_seconds: 600, run_deadline_seconds: null,
})

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', queueing: 'available' })
  vi.mocked(api.sharedCrewCapacity).mockResolvedValue(capacity('principal'))
  vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [] } as never)
  vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ items: [] } as never)
  vi.mocked(api.sharedCrewSendState).mockResolvedValue({ schema_version: 1, session_key: 's1', revision: 0, last: null } as never)
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewMode).mockResolvedValue(null)
  vi.mocked(api.sharedCrewPeople).mockResolvedValue(null)
})

const STATES = ['submitted', 'queued', 'admitted', 'running', 'cancelling', 'reconciling', 'completed', 'failed', 'cancelled', 'expired'] as const

describe('above the composer', () => {
  it.each(STATES)('shows no work status for %s work', async (state) => {
    workRead.mockResolvedValue({ items: [work({ state })] } as never)
    const { container } = mount(<SessionCollabStrip sessionKey="s1" />)
    await waitFor(() => expect(api.sharedCrewPresence).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('work-status-list')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('draws nothing of its own while others are present: presence lives in the context row', async () => {
    const me = person('p_me', 'Me')
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(me as never)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [{ principal: person('p_bob', 'Bob'), session_key: 's1', activity: 'typing', connections: 1, expires_at: 0 }] } as never)
    const { container } = mount(<SessionCollabStrip sessionKey="s1" />)
    await waitFor(() => expect(api.sharedCrewPresence).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(container).toBeEmptyDOMElement()
  })

  it.each(['presence', 'decisions', 'typing lease'] as const)('a failed %s read shows the error notice, not an empty strip', async (which) => {
    const failed = which === 'presence' ? api.sharedCrewPresence : which === 'decisions' ? api.sharedCrewDecisions : api.sharedCrewTypingLease
    vi.mocked(failed).mockRejectedValue(new Error(`${which} read failed`))
    mount(<SessionCollabStrip sessionKey="s1" />)
    expect(await screen.findByTestId('session-collab-error')).toHaveTextContent(`${which} read failed`)
  })
})

describe('queued send on its bubble', () => {
  it('a failed work read shows the error notice, not nothing', async () => {
    workRead.mockRejectedValue(new Error('work read failed'))
    mount(<QueuedSendLabel slotKey="s1" />)
    expect(await screen.findByTestId('queued-send-read-error')).toHaveTextContent('work read failed')
  })

  it('a failed capacity read beside a queued send shows the error notice', async () => {
    workRead.mockResolvedValue({ items: [work({})] } as never)
    vi.mocked(api.sharedCrewCapacity).mockRejectedValue(new Error('capacity read failed'))
    mount(<QueuedSendLabel slotKey="s1" />)
    expect(await screen.findByTestId('queued-send-capacity-error')).toHaveTextContent('capacity read failed')
    expect(screen.getByTestId('queued-send')).toBeInTheDocument()
  })

  it('picks the newest chat turn waiting for capacity', () => {
    expect(queuedChatTurn([work({ work_id: 'a' }), work({ work_id: 'b' }), work({ work_id: 'c', state: 'running' })])?.work_id).toBe('b')
    expect(queuedChatTurn([work({ operation: 'app_launch' as never })])).toBeNull()
    expect(queuedChatTurn([work({ state: 'completed' })])).toBeNull()
  })

  it('shows Queued with its own Cancel, naming whose limit holds it', async () => {
    workRead.mockResolvedValue({ items: [work({ reason: 'principal_active_limit' })] } as never)
    mount(<QueuedSendLabel slotKey="s1" />)
    expect(await screen.findByText('Queued')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Queued').closest('[title]')?.getAttribute('title')).toBe('Waiting on your limit'))
    expect(screen.getByRole('button', { name: 'Cancel this queued message' })).toBeInTheDocument()
  })

  it.each(['running', 'cancelling', 'completed', 'failed'] as const)('shows nothing for %s work', async (state) => {
    workRead.mockResolvedValue({ items: [work({ state })] } as never)
    const { container } = mount(<QueuedSendLabel slotKey="s1" />)
    await waitFor(() => expect(workRead).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(container).toBeEmptyDOMElement()
  })

  it('cancel calls the API once at the shown revision and the label goes away', async () => {
    workRead.mockResolvedValue({ items: [work({ revision: 1 })] } as never)
    let settle: (v: Awaited<ReturnType<typeof api.sharedCrewCancelWork>>) => void = () => {}
    cancel.mockReturnValue(new Promise(r => { settle = r }))
    mount(<QueuedSendLabel slotKey="s1" />)
    const btn = await screen.findByRole('button', { name: 'Cancel this queued message' })
    fireEvent.click(btn)
    fireEvent.click(btn)
    settle({ ok: true, work: work({ state: 'cancelled', revision: 2 }) })
    await waitFor(() => expect(screen.queryByTestId('queued-send')).toBeNull())
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalledWith('w1', 1)
  })

  it('a stale cancel adopts the server state and says so', async () => {
    workRead.mockResolvedValue({ items: [work({ revision: 1 })] } as never)
    cancel.mockResolvedValue({ ok: false, conflict: true, current: work({ state: 'running', revision: 5 }), code: 'stale_revision' })
    const { qc } = mount(<QueuedSendLabel slotKey="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel this queued message' }))
    expect(await screen.findByText('Not cancelled: it already started.')).toBeInTheDocument()
    expect((qc.getQueryData(['shared-crew', 'work', 's1']) as { items: AcceptedWork[] }).items[0].revision).toBe(5)
  })

  it.each(['admitted', 'running', 'cancelling', 'reconciling'] as const)('a stale cancel that finds the work %s says it already started', async (state) => {
    workRead.mockResolvedValue({ items: [work({ revision: 1 })] } as never)
    cancel.mockResolvedValue({ ok: false, conflict: true, current: work({ state, revision: 2 }), code: 'stale_revision' })
    mount(<QueuedSendLabel slotKey="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel this queued message' }))
    expect(await screen.findByText('Not cancelled: it already started.')).toBeInTheDocument()
  })

  it.each(['expired', 'completed', 'failed'] as const)('a stale cancel that finds the work %s says it ended, never that it started', async (state) => {
    workRead.mockResolvedValue({ items: [work({ revision: 1 })] } as never)
    cancel.mockResolvedValue({ ok: false, conflict: true, current: work({ state, revision: 2 }), code: 'stale_revision' })
    mount(<QueuedSendLabel slotKey="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel this queued message' }))
    expect(await screen.findByText('Not cancelled: it already ended.')).toBeInTheDocument()
    expect(screen.queryByText('Not cancelled: it already started.')).toBeNull()
  })

  it('a stale cancel on work someone else already cancelled does not claim it started', async () => {
    workRead.mockResolvedValue({ items: [work({ revision: 1 })] } as never)
    cancel.mockResolvedValue({ ok: false, conflict: true, current: work({ state: 'cancelled', revision: 2 }), code: 'stale_revision' })
    mount(<QueuedSendLabel slotKey="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel this queued message' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByTestId('queued-send')).toBeNull())
    expect(screen.queryByText('Not cancelled: it already started.')).toBeNull()
  })

  it('rides only the bubble the transcript flags, running turn or not', async () => {
    workRead.mockResolvedValue({ items: [work({})] } as never)
    const bubble = (show: boolean) => (
      <UserMessage content="hi" renderContent={c => <p>{c}</p>} slotKey="s1" slotRunning showQueuedSend={show} />
    )
    const { unmount } = mount(bubble(true))
    expect(await screen.findByTestId('queued-send')).toBeInTheDocument()
    unmount()
    mount(bubble(false))
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('queued-send')).toBeNull()
  })
})

describe('PresenceChips', () => {
  const entry = (id: string, name: string, activity: PresenceEntry['activity'], connections = 1): PresenceEntry =>
    ({ principal: person(id, name), session_key: 's1', activity, connections, expires_at: 0 })

  it('shows one chip per person and leaves the viewer out', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[
      entry('p_me', 'Me', 'typing', 3),
      entry('p_bob', 'Bob Example', 'typing', 4),
      entry('p_ann', 'Ann', 'viewing'),
    ]} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('names each chip by the person, with the name as its hover title and no text beside it', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[entry('p_bob', 'Bob Example', 'viewing')]} />)
    const chip = screen.getByLabelText('Bob Example')
    expect(chip).toHaveAttribute('title', 'Bob Example')
    expect(chip.textContent).toBe('BE')
    expect(chip).toHaveAttribute('tabindex', '0')
  })

  it('folds everyone past the third into +N', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[
      entry('p_a', 'Ann', 'viewing'), entry('p_b', 'Bob', 'viewing'), entry('p_c', 'Cy', 'viewing'), entry('p_d', 'Di', 'viewing'), entry('p_e', 'Ed', 'viewing'),
    ]} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(4)
    const more = screen.getByText('+2').closest('li')
    expect(more).toHaveAttribute('title', 'Di, Ed')
    expect(more).toHaveAttribute('tabindex', '0')
    expect(screen.queryByLabelText('Di')).toBeNull()
  })

  it('exactly three people draw three chips and no overflow', () => {
    mount(<PresenceChips viewer={null} entries={[entry('p_a', 'Ann', 'viewing'), entry('p_b', 'Bob', 'viewing'), entry('p_c', 'Cy', 'viewing')]} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.queryByText(/^\+/)).toBeNull()
  })

  it('never says who is typing: the locked composer says it once', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[entry('p_bob', 'Bob Example', 'typing')]} />)
    expect(screen.getByLabelText('Bob Example')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/typing/i)
    expect(document.body.innerHTML).not.toMatch(/typing/i)
  })

  it('handles a long name and an Unattributed person without breaking', () => {
    const long = 'A'.repeat(200)
    mount(<PresenceChips viewer={null} entries={[entry('p_l', long, 'viewing'), { ...entry('p_unknown', '', 'viewing'), principal: { id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } } }]} />)
    expect(screen.getByLabelText(long)).toBeInTheDocument()
    expect(screen.getByLabelText('Unattributed')).toBeInTheDocument()
  })

  it('renders nothing when nobody else is here', () => {
    const { container } = mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[entry('p_me', 'Me', 'viewing')]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

// The chips wait on capabilities, then mode and people, then the viewer and presence:
// each hop is its own query, so the waits that sit behind that chain get a longer bound.
const CHAIN_TIMEOUT_MS = 5000

describe('SessionPresence', () => {
  const bob = person('p_bob', 'Bob')
  const here = (p: ReturnType<typeof person>, activity: PresenceEntry['activity'] = 'viewing') =>
    ({ principal: p, session_key: 's1', activity, connections: 1, expires_at: 0 })
  const team = () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', shared_crew_preview: true })
    vi.mocked(api.sharedCrewMode).mockResolvedValue({ schema_version: 1, mode: 'network', crew_id: 'c1' } as never)
    vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: bob, sources: ['observed'], is_viewer: false }] } as never)
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(person('p_me', 'Me') as never)
  }

  it('draws one chip row for a teammate, however they are active', async () => {
    team()
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [here(person('p_me', 'Me')), here(bob, 'typing')] } as never)
    mount(<SessionPresence sessionKey="s1" />)
    const chips = await screen.findByTestId('presence-chips', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(chips.querySelectorAll('li')).toHaveLength(1)
    expect(screen.getAllByTestId('presence-chips')).toHaveLength(1)
  })

  it('draws nothing in a solo session, where only the viewer is present', async () => {
    team()
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [here(person('p_me', 'Me'))] } as never)
    const { container, qc } = mount(<SessionPresence sessionKey="s1" />)
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'presence', 's1'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'viewer'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    expect(container).toBeEmptyDOMElement()
  })

  it('draws nothing while the viewer read is pending, so the viewer never flashes as a teammate', async () => {
    team()
    const me = person('p_me', 'Me')
    let resolveViewer: (v: unknown) => void = () => {}
    vi.mocked(api.sharedCrewViewer).mockImplementation(() => new Promise(r => { resolveViewer = r }) as never)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [here(me)] } as never)
    const { container, qc } = mount(<SessionPresence sessionKey="s1" />)
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'presence', 's1'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    expect(container).toBeEmptyDOMElement()
    resolveViewer(me)
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'viewer'])).toBeDefined())
    expect(container).toBeEmptyDOMElement()
  })

  it('draws nothing while the Shared Crew preview is off', async () => {
    team()
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', shared_crew_preview: false })
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [here(bob)] } as never)
    const { container, qc } = mount(<SessionPresence sessionKey="s1" />)
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'presence', 's1'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    expect(container).toBeEmptyDOMElement()
  })
})

describe('shared decisions', () => {
  const decision = (state: SharedDecision['state'], outcome = ''): SharedDecision => ({
    decision_id: 'approval:2:s1:7', kind: 'approval', session_key: 's1', state, revision: 2,
    initiator: person('p_a', 'Alice'), responder: state === 'resolved' ? person('p_b', 'Bob') : null,
    outcome, resolved_at: state === 'resolved' ? 10 : null,
  })
  const question = (state: SharedDecision['state'], outcome = ''): SharedDecision => ({ ...decision(state, outcome), kind: 'question' })

  it('reads every settled verdict, for a person and for the viewer', () => {
    expect(resolvedByLine(decision('resolved', 'approved'))).toBe('Bob approved')
    expect(resolvedByLine(decision('resolved', 'rejected'))).toBe('Bob rejected')
    expect(resolvedByLine(question('resolved', 'answered'))).toBe('Bob answered')
    expect(resolvedByLine(decision('resolved', 'approved'), 'p_b')).toBe('You approved')
    expect(resolvedByLine(decision('resolved', 'rejected'), 'p_b')).toBe('You rejected')
    expect(resolvedByLine(question('resolved', 'answered'), 'p_b')).toBe('You answered')
  })

  it('reads a lost race as the winning decision, not a failure', () => {
    const err = { status: 409, body: JSON.stringify({ code: 'already_resolved', decision: decision('resolved', 'rejected') }) }
    const won = alreadyResolvedDecision(err)
    expect(won?.responder?.display.name).toBe('Bob')
    expect(resolvedByLine(won!)).toBe('Bob rejected')
    expect(alreadyResolvedDecision({ status: 404, body: '{}' })).toBeNull()
    expect(alreadyResolvedDecision({ status: 409, body: 'not json' })).toBeNull()
  })

  const local = (name: string) => person('p_local', name)
  const byLocal = (name: string): SharedDecision => ({ ...decision('resolved', 'approved'), initiator: local(name), responder: local(name) })

  it.each(['local-app', 'local-startup'])('never names the local token subject %s', (name) => {
    expect(resolvedByLine(byLocal(name))).toBe('Crew owner approved')
    expect(resolvedByLine(byLocal(name), 'p_local')).toBe('You approved')
  })

  it('names a teammate whose display name looks like the local token', () => {
    const mate = { ...byLocal('local-app'), responder: { id: 'p_t', kind: 'human' as const, display: { name: 'local-app', login: 'mate@example.com', captured_at: null } } }
    expect(resolvedByLine(mate)).toBe('local-app approved')
  })

  it('tells a lost race without a name', () => {
    expect(settledElsewhereLine(decision('resolved', 'approved'))).toBe('Already approved elsewhere')
    expect(settledElsewhereLine(decision('resolved', 'rejected'))).toBe('Already rejected elsewhere')
  })
})

describe('overload copy', () => {
  it('separates a personal limit from a Crew-wide one and says the draft is kept', () => {
    const mine = refusalMessage({ code: 'principal_active_limit', capacity: capacity('principal') })
    const crew = refusalMessage({ code: 'crew_active_limit', capacity: capacity('crew') })
    expect(mine).toBe("You're at your limit (2 of 2 running). Your draft is kept. Try again soon.")
    expect(crew).toBe('Crew is at its limit (8 of 8 running). Your draft is kept. Try again soon.')
    for (const s of [mine, crew]) expect(s).not.toMatch(/second|minute/i)
  })

  it('leaves non Shared Crew refusals to the caller', () => {
    expect(refusalMessage({ code: 'slot_busy' })).toBeNull()
    expect(refusalMessage({})).toBeNull()
  })

  it('falls back to the reason when no capacity is known', () => {
    expect(queuedReason('crew_queue_full', null)).toBe('Waiting on the Crew limit')
    expect(queuedReason(null, null)).toBe('Waiting')
  })
})

describe('send receipt', () => {
  const response = (status: number, body: unknown) => ({
    ok: status < 300, status, headers: { get: () => 'application/json' }, json: async () => body,
  }) as unknown as Response

  it('counts a host-accepted queued work item as accepted, not refused', async () => {
    const r = await readSendReceipt(response(202, { work: work({}) }))
    expect(r.outcome).toBe('accepted')
  })

  it('still reads a limit refusal as refused, so the draft is restored', async () => {
    const r = await readSendReceipt(response(429, { error: 'limit', code: 'principal_active_limit' }))
    expect(r.outcome).toBe('refused')
  })
})
