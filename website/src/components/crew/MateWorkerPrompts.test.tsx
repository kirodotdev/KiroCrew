/* A crewmate thread's chat shows the pending tool approvals and open question
 * cards of the sessions it dispatched (`mate_root`), and they are answered there
 * through the routes those sessions' own chats use. Crew Mode's stack is paired
 * against it: a Crew Mode root never shows a mate's session, nor a mate a crew
 * member. Rule: docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md
 * section 4. */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({
  crewWorkers: vi.fn(), crewWorkerApprove: vi.fn(), answerQuestion: vi.fn(), dismissQuestionCard: vi.fn(),
}))
vi.mock('../../api/client', async () => {
  const errors = await import('../../api/apiError')
  return { api, ApiError: errors.ApiError }
})
const sendTurn = vi.hoisted(() => vi.fn())
vi.mock('../../chat-core/transport/sendTurn', () => ({ sendTurn }))

import CrewApprovalCards, { MateApprovalCards, crewApprovalRequests } from './CrewApprovalCards'
import MateWorkerQuestions, { mateQuestionWorkers } from './MateWorkerQuestions'
import { __resetForTests as resetDrafts, loadDrafts } from '../../utils/chatDrafts'
import { ApiError } from '../../api/apiError'

const info = (key: string) => ({
  origin: 'native', request_id: `rid-${key}`, request_mid: `mid-${key}`,
  tool: 'Shell', tool_input: `ls ${key}`, tool_kind: 'execute', tool_purpose: 'list files', trust_grantable: '1',
})
/** A session waiting on a tool approval, under the given stamps. */
const waiting = (key: string, stamps: Record<string, unknown>) =>
  ({ key, title: `Session ${key}`, pending_approval: true, pending_approval_info: info(key), ...stamps })

const MATE = 'member-oncall'
const SLOTS = [
  { key: MATE, mode: 'member' },
  { key: 'member-research', mode: 'member' },
  { key: 'root', crew_mode_root: true },
  waiting('m1', { mate_root: MATE }),
  waiting('m2', { mate_root: 'member-research' }),
  waiting('c1', { crew_root: 'root' }),
  waiting('plain', {}),
]

async function mount(ui: React.ReactElement, slots: unknown[], chat: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots }, chat: { messages: [], slotMessages: {}, ...chat } } as never)
  await act(async () => {
    render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  })
  return store
}

const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

describe("a crewmate's chat: its sessions' tool approvals", () => {
  afterEach(() => vi.clearAllMocks())

  it('reads each tree by its own stamp: the mate tree by mate_root, Crew Mode by crew_root', () => {
    const tasks = new Map<string, string>()
    expect(crewApprovalRequests(SLOTS as never, MATE, tasks, 0, 'mate').map(r => r.worker)).toEqual(['m1'])
    expect(crewApprovalRequests(SLOTS as never, 'root', tasks, 0).map(r => r.worker)).toEqual(['c1'])
    // A mate thread is no Crew Mode root, and a conductor is no mate thread.
    expect(crewApprovalRequests(SLOTS as never, MATE, tasks, 0, 'crew')).toEqual([])
    expect(crewApprovalRequests(SLOTS as never, 'root', tasks, 0, 'mate')).toEqual([])
    expect(crewApprovalRequests(SLOTS as never, '', tasks, 0, 'mate')).toEqual([])
  })

  it("shows only this mate's session, worded for a session, and decides through the mate root", async () => {
    api.crewWorkers.mockResolvedValue({ root: MATE, root_kind: 'mate', workers: [{ key: 'm1', summary: 'Check the alarms' }] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    await mount(<MateApprovalCards root={MATE} />, SLOTS)
    const card = screen.getByTestId('crew-approval-card')
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
    expect(card).toHaveAttribute('data-worker', 'm1')
    expect(screen.getByRole('region', { name: "Tool approvals from this crewmate's sessions: 1" })).toBeInTheDocument()
    expect(await within(card).findByText('Check the alarms')).toBeInTheDocument()
    expect(within(card).getByText('Approves this and lets this session run tools without asking until Kiro Crew restarts.')).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('button', { name: 'Trust this session' }))
    await settle()
    // The owner-only crew route, named by the mate thread: no wider caller.
    expect(api.crewWorkerApprove).toHaveBeenCalledWith(MATE, 'm1', 'trust', 'rid-m1', 'mid-m1', '')
  })

  it.each([['Approve', 'approve'], ['Reject', 'reject']] as const)('%s pins the request the card showed', async (label, action) => {
    api.crewWorkers.mockResolvedValue({ root: MATE, workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    await mount(<MateApprovalCards root={MATE} />, SLOTS)
    fireEvent.click(screen.getByRole('button', { name: label }))
    await settle()
    expect(api.crewWorkerApprove).toHaveBeenCalledWith(MATE, 'm1', action, 'rid-m1', 'mid-m1', '')
  })

  it("reads no worker list while nothing of the mate's waits", async () => {
    await mount(<MateApprovalCards root={MATE} />, [{ key: MATE, mode: 'member' }, waiting('m2', { mate_root: 'member-research' })])
    expect(screen.queryByTestId('crew-approval-cards')).toBeNull()
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })

  it("a Crew Mode root's stack is unchanged: its own crew member only, crew wording", async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await mount(<CrewApprovalCards root="root" />, SLOTS)
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
    expect(screen.getByTestId('crew-approval-card')).toHaveAttribute('data-worker', 'c1')
    expect(screen.getByRole('button', { name: 'Trust this crew member' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Tool approvals from the crew: 1' })).toBeInTheDocument()
  })
})

const QUESTIONS = [{ question: 'Which region?', options: [{ label: 'us-east-1' }, { label: 'eu-west-1' }] }]

describe("a crewmate's chat: its sessions' question cards", () => {
  afterEach(() => { vi.clearAllMocks(); resetDrafts() })

  const slots = [
    { key: MATE, mode: 'member' },
    { key: 'm1', title: 'Deploy check', mate_root: MATE, needs_input: true },
    { key: 'm2', title: 'Other mate', mate_root: 'member-research', needs_input: true },
    { key: 'c1', title: 'Crew member', crew_root: 'root', needs_input: true },
  ]
  const cards = (m1: Record<string, unknown>) => ({
    pendingQuestions: {
      m1: { slot: 'm1', questions: QUESTIONS, ...m1 },
      m2: { slot: 'm2', ask_id: 'ask-m2', questions: QUESTIONS },
      c1: { slot: 'c1', ask_id: 'ask-c1', questions: QUESTIONS },
    },
  })

  it("lists only this mate's sessions with an open card", () => {
    const pending = cards({ ask_id: 'ask-m1' }).pendingQuestions
    expect(mateQuestionWorkers(slots as never, MATE, pending as never).map(s => s.key)).toEqual(['m1'])
    expect(mateQuestionWorkers(slots as never, '', pending as never)).toEqual([])
  })

  it("answers a blocking ask through the session's own answer route", async () => {
    api.answerQuestion.mockResolvedValue({ ok: true })
    const store = await mount(<MateWorkerQuestions root={MATE} />, slots, cards({ ask_id: 'ask-m1' }))
    const block = screen.getByTestId('mate-worker-question')
    expect(screen.getAllByTestId('mate-worker-question')).toHaveLength(1)
    expect(block).toHaveAttribute('data-worker', 'm1')
    expect(within(block).getByRole('heading', { name: 'Deploy check asks' })).toBeInTheDocument()
    fireEvent.click(within(block).getByText('us-east-1'))
    fireEvent.click(within(block).getByText('Submit'))
    await settle()
    expect(api.answerQuestion).toHaveBeenCalledWith('ask-m1', { 'Which region?': 'us-east-1' })
    expect(sendTurn).not.toHaveBeenCalled()
    await waitFor(() => expect(store.getState().chat.pendingQuestions?.m1).toBeUndefined())
  })

  it('answers a card nothing waits on with a message to that session, steering a native one into its turn', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    const running = slots.map(s => s.key === 'm1' ? { ...s, running: true } : s)
    await mount(<MateWorkerQuestions root={MATE} />, running, cards({ serverCardId: 'card-1', native: true }))
    const block = screen.getByTestId('mate-worker-question')
    fireEvent.click(within(block).getByText('eu-west-1'))
    fireEvent.click(within(block).getByText('Submit'))
    await settle()
    expect(sendTurn).toHaveBeenCalledWith({ slot: 'm1', message: 'eu-west-1', steer: true })
    expect(api.answerQuestion).not.toHaveBeenCalled()
  })

  it("sends a native card answered while its session is idle as a plain message", async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    await mount(<MateWorkerQuestions root={MATE} />, slots, cards({ serverCardId: 'card-1', native: true }))
    fireEvent.click(screen.getByText('us-east-1'))
    fireEvent.click(screen.getByText('Submit'))
    await settle()
    expect(sendTurn).toHaveBeenCalledWith({ slot: 'm1', message: 'us-east-1' })
  })

  it("says so here when the answer is refused, and puts it back in the session's own composer", async () => {
    sendTurn.mockResolvedValue({ status: 'refused', body: {}, reason: 'no' })
    const store = await mount(<MateWorkerQuestions root={MATE} />, slots, cards({ serverCardId: 'card-1' }))
    fireEvent.click(screen.getByText('us-east-1'))
    fireEvent.click(screen.getByText('Submit'))
    await settle()
    expect(sendTurn).toHaveBeenCalledWith({ slot: 'm1', message: 'us-east-1' })
    const notice = await screen.findByTestId('mate-worker-question-failure')
    expect(notice).toHaveTextContent("Your answer to Deploy check did not send. It is back in Deploy check's message box.")
    // No composer is mounted here: the text waits in the session's saved draft,
    // which its main chat and its panes both open with.
    expect(loadDrafts().m1).toBe('us-east-1')
    const rows = store.getState().chat.slotMessages?.m1 ?? []
    expect(rows.some((m: { role: string }) => m.role === 'error')).toBe(true)
  })

  it.each(['response-late', 'unknown'] as const)('a %s receipt may have landed: it is not offered again', async (status) => {
    sendTurn.mockResolvedValue({ status, body: {} })
    const store = await mount(<MateWorkerQuestions root={MATE} />, slots, cards({ serverCardId: 'card-1' }))
    fireEvent.click(screen.getByText('us-east-1'))
    fireEvent.click(screen.getByText('Submit'))
    await settle()
    expect(await screen.findByTestId('mate-worker-question-failure')).toHaveTextContent('Your answer to Deploy check may not have arrived.')
    expect(loadDrafts().m1).toBeUndefined()
    expect(store.getState().chat.slotMessages?.m1 ?? []).toEqual([])
  })

  it("a blocking ask whose wait is gone parks the answer in the session's composer", async () => {
    api.answerQuestion.mockRejectedValue(new ApiError(404, 'gone', JSON.stringify({ error: 'gone', code: 'question_not_found' })))
    await mount(<MateWorkerQuestions root={MATE} />, slots, cards({ ask_id: 'ask-m1' }))
    fireEvent.click(screen.getByText('us-east-1'))
    fireEvent.click(screen.getByText('Submit'))
    await settle()
    expect(api.answerQuestion).toHaveBeenCalledWith('ask-m1', { 'Which region?': 'us-east-1' })
    expect(await screen.findByTestId('mate-worker-question-failure')).toBeInTheDocument()
    expect(loadDrafts().m1).toBe('us-east-1')
  })

  it("keeps an expired ask's notice in view, so the person learns where the answer went", async () => {
    const notice = { message: 'Your answer was put back in the message box.', kind: 'restored' }
    const pending = cards({ ask_id: 'ask-m1' }).pendingQuestions as Record<string, unknown>
    delete pending.m1
    expect(mateQuestionWorkers(slots as never, MATE, pending as never, { m1: notice }).map(s => s.key)).toEqual(['m1'])
    await mount(<MateWorkerQuestions root={MATE} />, slots, { pendingQuestions: pending, restoredQuestionNotices: { m1: notice } })
    expect(screen.getByTestId('mate-worker-question')).toHaveAttribute('data-worker', 'm1')
    expect(screen.getByTestId('pending-question-notice')).toHaveTextContent('Your answer was put back in the message box.')
  })

  it('renders nothing when no session of the mate asks', async () => {
    await mount(<MateWorkerQuestions root={MATE} />, slots, { pendingQuestions: {} })
    expect(screen.queryByTestId('mate-worker-questions')).toBeNull()
  })
})
