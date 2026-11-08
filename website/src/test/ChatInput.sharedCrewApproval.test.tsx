/**
 * A pending approval or question says whose turn raised it, in one word, when
 * that is someone else; and when another person settles the card it says how it
 * ended, once, on the composer's status line. Nothing here is a transcript row.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { api } from '../api/client'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import { applySharedCrewFrame, sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import type { PublicPrincipal, SharedDecision } from '../api/sharedCrew'
import { resolveByApprovalId, setQuestionCard } from '../store/chatSlice'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') { super(message); this.status = status; this.body = body }
  }
  return { api: {
    resolveApproval: vi.fn(), approveChatSlot: vi.fn(),
    sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewViewer: vi.fn(), sharedCrewDecisions: vi.fn(),
  }, ApiError: MockApiError }
})

// The card label waits on capabilities, then mode and people, then the viewer and decisions:
// each hop is its own query, so the waits that sit behind that chain get a longer bound.
const CHAIN_TIMEOUT_MS = 5000
const person = (id: string, name: string) => ({ id, kind: 'human', display: { name, login: '', captured_at: null } }) as PublicPrincipal
const bob = person('p_bob', 'Bob')
const alice = person('p_alice', 'Alice Example')
const decision = (over: Partial<SharedDecision>): SharedDecision => ({
  decision_id: 'approval:3:slot-1:ap-1', kind: 'approval', session_key: 'slot-1', state: 'pending', revision: 1,
  initiator: alice, responder: null, outcome: '', resolved_at: null, ...over,
})
const permission = (id: string) => ({ role: 'permission', content: 'Running: ls', ts: id, meta: { approval_id: id, tool_call_id: `tc-${id}`, tool_input: '{}' } })

function makeStore(rows: unknown[]) {
  return createTestStore({
    chat: { activeSlot: 'slot-1', messages: [{ role: 'user', content: 'x' }, ...rows], slotMessages: {}, toolLog: [], slotActivity: {}, slotStatusDetail: {} } as unknown as RootState['chat'],
    dashboard: { slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true }], approvalMode: 'normal', connected: true, refreshTrigger: 0, unreadSlots: [] } as unknown as RootState['dashboard'],
  })
}

/** The card's own hooks only read the decisions cache, which the chat's collab strip fills; `items`
 *  stands in for that read (`null` leaves the cache empty, as before the first frame). */
function mountCard(rows: unknown[], items: SharedDecision[] | null = [decision({})]) {
  const store = makeStore(rows)
  const { queryClient: qc } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} /></SlotProvider>, { store })
  if (items) qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items })
  return { store, qc }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', shared_crew_preview: true })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({ schema_version: 1, mode: 'network', crew_id: 'c1' } as never)
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: alice, sources: ['observed'], is_viewer: false }] } as never)
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(bob)
})

describe('a pending approval card', () => {
  it("says whose turn raised it, in a short visible label", async () => {
    mountCard([permission('ap-1')])
    const pill = await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(pill).toHaveTextContent(/^Raised by Alice Example$/)
    expect(screen.getAllByTestId('raised-by')).toHaveLength(1)
  })

  it.each([
    ['the viewer raised it', decision({ initiator: bob })],
    ['the gateway answers it by its approval mode', decision({ settled_by: 'host' })],
    ['it is another card', decision({ decision_id: 'approval:3:slot-1:ap-9' })],
  ])('names nobody when %s', async (_why, item) => {
    const { qc } = mountCard([permission('ap-1')], [item])
    await screen.findByText('Allow once')
    await waitFor(() => expect(qc.getQueryData(sharedCrewKeys.decisions('slot-1'))).toBeDefined())
    await waitFor(() => expect(qc.getQueryData(sharedCrewKeys.viewer)).toBeDefined())
    expect(screen.queryByTestId('raised-by')).toBeNull()
  })
})

describe('a card settled by someone else', () => {
  const settled = decision({ state: 'resolved', outcome: 'approved', responder: alice, resolved_at: 5 })

  it('says how it ended once, and clears when the next card arrives', async () => {
    const { qc, store } = mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [settled] }) })
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getAllByText('Alice Example approved')).toHaveLength(1)
    const strip = screen.getByRole('status')
    const icon = strip.querySelector('svg')?.getAttribute('class') ?? ''
    expect(icon).toMatch(/check/)
    expect(icon).not.toMatch(/lock/)
    await act(async () => { store.dispatch({ type: 'chat/appendMessage', payload: permission('ap-2') }) })
    await waitFor(() => expect(screen.queryByText('Alice Example approved')).toBeNull())
  })

  it('clears when a question card becomes the next card', async () => {
    const { qc, store } = mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [settled] }) })
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    await act(async () => { store.dispatch(resolveByApprovalId({ id: 'ap-1', slot: 'slot-1', decision: 'approved' })) })
    expect(screen.getByText('Alice Example approved')).toBeInTheDocument()
    await act(async () => { store.dispatch(setQuestionCard({ slot: 'slot-1', ask_id: 'ask-9', questions: [{ question: 'Pick', options: [{ label: 'A' }] }] } as never)) })
    await waitFor(() => expect(screen.queryByText('Alice Example approved')).toBeNull())
  })

  it('keeps the line when a question card was already on screen beside the settled approval', async () => {
    const { qc, store } = mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => { store.dispatch(setQuestionCard({ slot: 'slot-1', ask_id: 'ask-9', questions: [{ question: 'Pick', options: [{ label: 'A' }] }] } as never)) })
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [settled] }) })
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    await act(async () => { store.dispatch(resolveByApprovalId({ id: 'ap-1', slot: 'slot-1', decision: 'approved' })) })
    expect(screen.getByText('Alice Example approved')).toBeInTheDocument()
  })

  it('does not follow the viewer into another session', async () => {
    const store = makeStore([permission('ap-1')])
    const ui = (slot: string) => <SlotProvider slotId={slot}><ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} /></SlotProvider>
    const { queryClient: qc, rerender } = renderWithProviders(ui('slot-1'), { store })
    qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [decision({})] })
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [settled] }) })
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    rerender(ui('slot-2'))
    await waitFor(() => expect(screen.queryByText('Alice Example approved')).toBeNull())
  })

  it('reports a card settled before any decisions were read, from the frame alone', async () => {
    const { qc } = mountCard([permission('ap-1')], null)
    await waitFor(() => expect(qc.getQueryData(sharedCrewKeys.viewer)).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    act(() => { applySharedCrewFrame(qc, { type: 'shared_crew.decision', schema_version: 1, decision: settled }) })
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
  })

  it('never reads the decisions itself: the collab strip owns that read and its failure', async () => {
    mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(api.sharedCrewDecisions).not.toHaveBeenCalled()
  })

  it.each([
    ['a rejection', { outcome: 'rejected' }, false],
    ['an approval', { outcome: 'approved' }, true],
  ] as const)('shows %s from someone else with the matching icon', async (_why, over, check) => {
    const { qc } = mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [{ ...settled, ...over }] }) })
    const line = await screen.findByText(/Alice Example (approved|rejected)/, {}, { timeout: CHAIN_TIMEOUT_MS })
    const icon = line.parentElement?.querySelector('svg')?.getAttribute('class') ?? ''
    if (check) expect(icon).toMatch(/check/)
    else expect(icon).not.toMatch(/check/)
  })

  it('a click that loses the race shows the line once, and the frame that follows adds no second', async () => {
    const { ApiError } = await import('../api/client')
    const body = JSON.stringify({ code: 'already_resolved', decision: settled })
    vi.mocked(api.resolveApproval).mockRejectedValue(new (ApiError as unknown as new (s: number, m: string, b: string) => Error)(409, 'conflict', body))
    const { qc } = mountCard([permission('ap-1')])
    await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(qc.getQueryData(sharedCrewKeys.viewer)).toBeDefined())
    fireEvent.click(screen.getByText('Allow once'))
    expect(await screen.findByText('Alice Example approved', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-1'), { schema_version: 1, items: [settled] }) })
    expect(screen.getAllByText('Alice Example approved')).toHaveLength(1)
  })
})
