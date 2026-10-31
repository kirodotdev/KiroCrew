import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AcceptedWork, CapacityResponse, PresenceEntry, SharedDecision } from '../../../api/sharedCrew'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import UserMessage from '../UserMessage'
import { QueuedSendLabel, queuedChatTurn } from './QueuedSendLabel'
import { SessionCollabStrip } from './SessionCollabStrip'
import { PresenceChips } from './PresenceChips'
import { DecisionAttributionList } from './DecisionAttributionList'
import { refusalMessage, queuedReason } from './overloadCopy'
import { alreadyResolvedDecision, requestedByLine, resolvedByLine, settledElsewhereLine } from '../../../components/chat-input/sharedDecision'
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
    expect(screen.queryByTestId('session-collab-strip')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('draws no strip when only the viewer is present', async () => {
    const me = { id: 'p_me', kind: 'human', display: { name: 'Me', login: '', captured_at: null } }
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(me as never)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [{ principal: me, session_key: 's1', activity: 'viewing', connections: 1, expires_at: 0 }] } as never)
    const { container } = mount(<SessionCollabStrip sessionKey="s1" />)
    await waitFor(() => expect(api.sharedCrewPresence).toHaveBeenCalled())
    await waitFor(() => expect(api.sharedCrewViewer).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('session-collab-strip')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('shows no presence chip while the viewer read is still pending', async () => {
    const me = { id: 'p_me', kind: 'human', display: { name: 'Me', login: '', captured_at: null } }
    let resolveViewer: (v: unknown) => void = () => {}
    vi.mocked(api.sharedCrewViewer).mockImplementation(() => new Promise(r => { resolveViewer = r }) as never)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ entries: [{ principal: me, session_key: 's1', activity: 'viewing', connections: 1, expires_at: 0 }] } as never)
    const { container } = mount(<SessionCollabStrip sessionKey="s1" />)
    await waitFor(() => expect(api.sharedCrewPresence).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByTestId('presence-chips')).toBeNull()
    expect(container).toBeEmptyDOMElement()
    resolveViewer(me)
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
    await waitFor(() => expect(screen.getByText('Queued').closest('[title]')?.getAttribute('title')).toMatch(/your personal limit/))
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
    expect(await screen.findByText(/changed before the cancel landed/)).toBeInTheDocument()
    expect((qc.getQueryData(['shared-crew', 'work', 's1']) as { items: AcceptedWork[] }).items[0].revision).toBe(5)
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

  it('shows one chip per person, leaves the viewer out, and names who types', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[
      entry('p_me', 'Me', 'typing', 3),
      entry('p_bob', 'Bob Example', 'typing', 4),
      entry('p_ann', 'Ann', 'viewing'),
    ]} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByTestId('presence-typing').textContent).toBe('Bob Example is typing')
  })

  it('handles a long name and an Unattributed person without breaking', () => {
    const long = 'A'.repeat(200)
    mount(<PresenceChips viewer={null} entries={[entry('p_l', long, 'viewing'), { ...entry('p_unknown', '', 'viewing'), principal: { id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } } }]} />)
    expect(screen.getByLabelText(`${long} is here`)).toBeInTheDocument()
    expect(screen.getByLabelText('Unattributed is here')).toBeInTheDocument()
  })

  it('renders nothing when nobody else is here', () => {
    const { container } = mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[entry('p_me', 'Me', 'viewing')]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('shared decisions', () => {
  const decision = (state: SharedDecision['state'], outcome = ''): SharedDecision => ({
    decision_id: 'approval:2:s1:7', kind: 'approval', session_key: 's1', state, revision: 2,
    initiator: person('p_a', 'Alice'), responder: state === 'resolved' ? person('p_b', 'Bob') : null,
    outcome, resolved_at: state === 'resolved' ? 10 : null,
  })

  it('names both the person whose turn asked and the person who answered', () => {
    mount(<DecisionAttributionList items={[decision('resolved', 'approved')]} />)
    expect(screen.getByText('Approved by Bob')).toBeInTheDocument()
    expect(screen.getByText(/asked during Alice's turn/)).toBeInTheDocument()
  })

  it('lets a long responder name shrink so the requester stays on the row', () => {
    mount(<DecisionAttributionList items={[decision('resolved', 'approved')]} />)
    const head = screen.getByText('Approved by Bob')
    expect(head).toHaveClass('truncate', 'min-w-0')
    expect(head).not.toHaveClass('shrink-0')
    expect(head).toHaveAttribute('title', 'Approved by Bob')
  })

  it('reads a lost race as the winning decision, not a failure', () => {
    const err = { status: 409, body: JSON.stringify({ code: 'already_resolved', decision: decision('resolved', 'rejected') }) }
    const won = alreadyResolvedDecision(err)
    expect(won?.responder?.display.name).toBe('Bob')
    expect(resolvedByLine(won!)).toBe('Rejected by Bob')
    expect(alreadyResolvedDecision({ status: 404, body: '{}' })).toBeNull()
    expect(alreadyResolvedDecision({ status: 409, body: 'not json' })).toBeNull()
  })

  const local = (name: string) => person('p_local', name)
  const byLocal = (name: string): SharedDecision => ({ ...decision('resolved', 'approved'), initiator: local(name), responder: local(name) })
  const mode = (m: 'personal' | 'network') => ({ schema_version: 1, mode: m, crew_id: 'c1' }) as never
  // The strip reads capabilities first; only then are the mode, viewer and
  // decisions queries enabled. Waits that span that chain get this ceiling.
  const CHAIN_TIMEOUT_MS = 5000

  it('shows no attribution line in a personal Crew', async () => {
    vi.mocked(api.sharedCrewMode).mockResolvedValue(mode('personal'))
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ items: [byLocal('local-app')] } as never)
    const { qc } = mount(<SessionCollabStrip sessionKey="s1" />)
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'mode'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(qc.getQueryData(['shared-crew', 'decisions', 's1'])).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('decision-attribution')).toBeNull()
    expect(screen.queryByText(/local-app/)).toBeNull()
  })

  it('keeps the line in a shared Crew and reads the viewer as you', async () => {
    vi.mocked(api.sharedCrewMode).mockResolvedValue(mode('network'))
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(local('local-app') as never)
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ items: [byLocal('local-app')] } as never)
    mount(<SessionCollabStrip sessionKey="s1" />)
    expect(await screen.findByText('Approved by you', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByText(/asked during your turn/)).toBeInTheDocument()
    expect(screen.queryByText(/local-app/)).toBeNull()
  })

  it.each(['local-app', 'local-startup'])('never names the local token subject %s', (name) => {
    expect(resolvedByLine(byLocal(name))).toBe('Approved by Crew owner')
    expect(requestedByLine(byLocal(name))).toBe("asked during Crew owner's turn")
    expect(resolvedByLine(byLocal(name), 'p_local')).toBe('Approved by you')
  })

  it('names a teammate whose display name looks like the local token', () => {
    const mate = { ...byLocal('local-app'), responder: { id: 'p_t', kind: 'human' as const, display: { name: 'local-app', login: 'mate@example.com', captured_at: null } } }
    expect(resolvedByLine(mate)).toBe('Approved by local-app')
  })

  it('tells a lost race without a name', () => {
    expect(settledElsewhereLine(decision('resolved', 'approved'))).toBe('Already approved elsewhere')
    expect(settledElsewhereLine(decision('resolved', 'rejected'))).toBe('Already rejected elsewhere')
  })
})

describe('overload copy', () => {
  it('separates a personal limit from a Crew-wide one and keeps the draft promise', () => {
    const mine = refusalMessage({ code: 'principal_active_limit', capacity: capacity('principal') })
    const crew = refusalMessage({ code: 'crew_active_limit', capacity: capacity('crew') })
    expect(mine).toMatch(/personal limit \(2 of 2 running\)/)
    expect(crew).toMatch(/limit for everyone \(8 of 8 running\)/)
    for (const s of [mine, crew]) {
      expect(s).toMatch(/still in the composer/)
      expect(s).not.toMatch(/second|minute/i)
    }
  })

  it('leaves non Shared Crew refusals to the caller', () => {
    expect(refusalMessage({ code: 'slot_busy' })).toBeNull()
    expect(refusalMessage({})).toBeNull()
  })

  it('falls back to the reason when no capacity is known', () => {
    expect(queuedReason('crew_queue_full', null)).toMatch(/shared limit/)
    expect(queuedReason(null, null)).toBe('waiting for its turn')
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
