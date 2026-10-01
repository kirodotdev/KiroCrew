import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import AttentionCard from '../pages/chat/command-center/AttentionCard'
import { api, ApiError } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import { buildCommandCenter, type AttentionItem } from '../pages/chat/command-center/model'

const approval: AttentionItem = { id: 'approval:child:r1', kind: 'approval', slot: 'child', native: true, approvalMode: 'normal', approval: { id: 'r1', instance: 'inst-r1', request_mid: 'row-r1', slot: 'dashboard:child', tool: 'shell', tool_input: 'git status' } }
const question: AttentionItem = { id: 'question:q1', kind: 'question', slot: 'child', question: { slot: 'child', ask_id: 'q1', questions: [{ question: 'Which scope?', options: [{ label: 'Backend' }, { label: 'Frontend' }] }] } }

describe('task dashboard input routing', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('leads with each requested action when two requests belong to the same session', () => {
    renderWithProviders(<>
      <AttentionCard item={approval} title="Release session" />
      <AttentionCard item={{ ...approval, id: 'second', approval: { ...approval.approval!, id: 'r2', tool_input: 'npm test' } }} title="Release session" />
    </>)
    const headings = screen.getAllByRole('heading', { level: 3 })
    expect(headings).toHaveLength(2)
    expect(headings[0]).not.toHaveTextContent('Release session')
    expect(headings[0].textContent).not.toBe(headings[1].textContent)
    expect(screen.getAllByText('From session: Release session')).toHaveLength(2)
    expect(screen.queryByText('Release session')).not.toBeInTheDocument()
    expect(screen.getAllByRole('region', { name: 'Approval required' }).map(node => node.textContent)).toEqual(['git status', 'npm test'])
  })

  it.each([true, false])('shows only the stated purpose and truthful rejection semantics (native=%s)', native => {
    renderWithProviders(<AttentionCard item={{ ...approval, native, approval: { ...approval.approval!, tool_purpose: 'Inspect the isolated release workspace' } }} title="Worker" />)
    expect(screen.getByText('Inspect the isolated release workspace')).toBeVisible()
    expect(screen.getByText('Normal asks before tools that require approval.')).toBeVisible()
    expect(screen.getByText('Responding here resolves this request. Other views show the result when refreshed.')).toBeVisible()
    expect(screen.getByText('Reject once refuses this request. Permission mode stays the same.')).toBeVisible()
  })

  it('explicitly reports missing purpose instead of substituting session context', () => {
    renderWithProviders(<AttentionCard item={approval} title="Worker" context="Release the product" />)
    expect(screen.getByText('The agent did not provide a reason for this request.')).toBeVisible()
  })

  it('does not auto-approve in Normal mode and routes one explicit click to the exact slot/request', async () => {
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={approval} title="Backend worker" />)
    expect(approve).not.toHaveBeenCalled()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'approved', { request_id: 'r1', request_mid: 'row-r1', origin: 'native' }))
    expect(resolve).not.toHaveBeenCalled()
    expect(await screen.findByText('Your response was recorded.')).toBeInTheDocument()
  })

  it.each([true, false])('rejects only this request without changing permission mode (native=%s)', async (native) => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={{ ...approval, native }} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Reject once' }))
    if (native) {
      await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'rejected_once', { request_id: 'r1', request_mid: 'row-r1', origin: 'native' }))
      expect(resolve).not.toHaveBeenCalled()
    } else {
      await waitFor(() => expect(resolve).toHaveBeenCalledWith('r1', 'reject_once', { origin: 'coordinator', slot: 'dashboard:child', instance: 'inst-r1' }))
      expect(approve).not.toHaveBeenCalled()
    }
  })

  it('approves only the coordinator origin and recorded slot, not a normalized native identity', async () => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    const approve = vi.spyOn(api, 'approveChatSlot')
    renderWithProviders(<AttentionCard item={{ ...approval, native: false }} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(resolve).toHaveBeenCalledWith('r1', 'approve', { origin: 'coordinator', slot: 'dashboard:child', instance: 'inst-r1' }))
    expect(approve).not.toHaveBeenCalled()
  })

  it.each([true, false])('retires stale origin-bound approvals without falling back (native=%s)', async native => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockRejectedValue(new ApiError(404, 'expired'))
    const approve = vi.spyOn(api, 'approveChatSlot').mockRejectedValue(new ApiError(404, 'expired'))
    renderWithProviders(<AttentionCard item={{ ...approval, native }} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument())
    expect(native ? resolve : approve).not.toHaveBeenCalled()
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
  })

  it('serializes origin/session/request selectors and leaves legacy resolution unchanged', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await api.resolveApproval('request/id', 'approve', { origin: 'coordinator', slot: 'slack:thread/id', instance: 'inst-1' })
    let [url, init] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/approvals/request%2Fid/approve?origin=coordinator&slot=slack%3Athread%2Fid&instance=inst-1')
    expect(init.method).toBe('POST')
    await api.resolveApproval('legacy', 'reject_once')
    expect(fetch.mock.calls[1][0]).toBe('/api/approvals/legacy/reject_once')
    await api.approveChatSlot('slot/id', 'rejected_once', { request_id: 'request/id', request_mid: 'row-wire', origin: 'native' })
    ;[url, init] = fetch.mock.calls[2] as [string, RequestInit]
    expect(url).toBe('/api/chat/slots/slot%2Fid/approve')
    expect(JSON.parse(init.body as string)).toEqual({ action: 'rejected_once', request_id: 'request/id', request_mid: 'row-wire', origin: 'native' })
  })

  it('keeps a generic conflict retryable without switching origin or claiming success', async () => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockRejectedValue(new ApiError(409, 'Conflict'))
    const approve = vi.spyOn(api, 'approveChatSlot')
    renderWithProviders(<AttentionCard item={{ ...approval, native: false }} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await screen.findByText('Conflict')
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeEnabled()
    expect(resolve).toHaveBeenCalledTimes(1)
    expect(approve).not.toHaveBeenCalled()
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
  })

  it.each(['recorded', 'expired'])('keeps a colliding native command actionable after the coordinator is %s', async outcome => {
    const resolve = vi.spyOn(api, 'resolveApproval')
    if (outcome === 'recorded') resolve.mockResolvedValue({ ok: true })
    else resolve.mockRejectedValue(new ApiError(404, 'expired'))
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    const card = (coordinator: boolean) => {
      const item = buildCommandCenter({ root: 'child',
        slots: [{ key: 'child', messages: 0, running: true, pending_approval: true, pending_approval_info: { origin: coordinator ? 'coordinator' : 'native', request_mid: 'row-same', request_id: 'same', tool: 'shell', tool_input: 'native command', tool_kind: 'execute' } }],
        subagents: {}, workflows: [], questions: [],
        approvals: coordinator ? [{ id: 'same', instance: 'inst-same', slot: 'dashboard:child', tool: 'shell', tool_input: 'coordinator command' }] : [],
      }).attention[0]
      return <AttentionCard key={item.id} item={item} title="Worker" />
    }
    const { rerender } = renderWithProviders(card(true))
    expect(screen.getByRole('region', { name: 'Approval required' })).toHaveTextContent('coordinator command')
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument())
    rerender(card(true))
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    rerender(card(false))
    expect(screen.getByRole('region', { name: 'Approval required' })).toHaveTextContent('native command')
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'approved', { request_id: 'same', request_mid: 'row-same', origin: 'native' }))
    expect(resolve).toHaveBeenCalledTimes(1)
  })

  it('does not carry a delivered state into a reused native request ID', async () => {
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    const card = (request_mid: string) => {
      const [item] = buildCommandCenter({ root: 'child', slots: [{ key: 'child', messages: 0, running: true, pending_approval: true,
        pending_approval_info: { origin: 'native', request_id: 'same', request_mid, tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' } }],
      subagents: {}, workflows: [], questions: [], approvals: [] }).attention
      return <AttentionCard key={item.id} item={item} title="Worker" />
    }
    const { rerender } = renderWithProviders(card('old-row'))
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await screen.findByText('Your response was recorded.')
    rerender(card('replacement-row'))
    fireEvent.click(screen.getByRole('button', { name: 'Reject once' }))
    await waitFor(() => expect(approve).toHaveBeenLastCalledWith('child', 'rejected_once', { request_id: 'same', request_mid: 'replacement-row', origin: 'native' }))
    expect(approve).toHaveBeenCalledTimes(2)
  })

  it('renders simultaneous colliding origins and routes each button to its own registry', async () => {
    const resolve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({ ok: true })
    const approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
    const items = buildCommandCenter({ root: 'child',
      slots: [{ key: 'child', messages: 0, running: true, pending_approval: true, pending_approval_info: { origin: 'native', request_mid: 'row-same', request_id: 'same', tool: 'native tool', tool_input: 'native command', tool_kind: 'execute' } }],
      subagents: {}, workflows: [], questions: [],
      approvals: [{ id: 'same', instance: 'inst-same', slot: 'dashboard:child', tool: 'coordinator tool', tool_input: 'coordinator command' }],
    }).attention
    renderWithProviders(<>{items.map(item => <AttentionCard key={item.id} item={item} title="Worker" />)}</>)
    expect(screen.getAllByRole('button', { name: 'Approve once' })).toHaveLength(2)
    const nativeCard = screen.getByText('native command').closest('section')!
    const coordinatorCard = screen.getByText('coordinator command').closest('section')!
    fireEvent.click(within(nativeCard).getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child', 'approved', { request_id: 'same', request_mid: 'row-same', origin: 'native' }))
    expect(resolve).not.toHaveBeenCalled()
    expect(within(coordinatorCard).getByRole('button', { name: 'Reject once' })).toBeEnabled()
    fireEvent.click(within(coordinatorCard).getByRole('button', { name: 'Reject once' }))
    await waitFor(() => expect(resolve).toHaveBeenCalledWith('same', 'reject_once', { origin: 'coordinator', slot: 'dashboard:child', instance: 'inst-same' }))
    expect(approve).toHaveBeenCalledTimes(1)
  })

  it.each([[undefined, false], ['coordinator', false], [undefined, true], ['native', false]] as const)('offers Open session without guessed permission buttons for origin=%s, inventory=%s', (origin, inventory) => {
    const approve = vi.spyOn(api, 'approveChatSlot')
    const resolve = vi.spyOn(api, 'resolveApproval')
    const [item] = buildCommandCenter({ root: 'child',
      slots: [{ key: 'child', messages: 0, running: true, pending_approval: true, pending_approval_info: { origin, request_id: 'same', tool: 'shell', tool_input: 'command', tool_kind: 'execute' } }],
      subagents: {}, workflows: [], questions: [], approvals: inventory ? [{ id: 'same', slot: 'dashboard:child' }] : [],
    }).attention
    renderWithProviders(<AttentionCard item={item} title="Worker" />)
    expect(screen.getByRole('link', { name: 'Open session' })).toHaveAttribute('href', '/chat?sid=child')
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject once' })).not.toBeInTheDocument()
    expect(screen.getByText('This view cannot verify the request. Open the session to review and respond.')).toBeVisible()
    expect(screen.queryByText(/Approval required|Permission mode:|Normal asks before tools|Only your explicit submission/)).not.toBeInTheDocument()
    expect(approve).not.toHaveBeenCalled()
    expect(resolve).not.toHaveBeenCalled()
  })

  it('keeps the complete approval command verbatim in a focusable scrolling preview', () => {
    const command = JSON.stringify({ command: `git show --format=%H ${'very-long-ref-'.repeat(30)}\\n` })
    const { container } = renderWithProviders(<AttentionCard item={{ ...approval, approval: { ...approval.approval!, tool_input: command } }} title="Worker" />)
    const preview = container.querySelector('pre')!
    expect(preview.textContent).toBe(command)
    expect(preview).toHaveAttribute('tabindex', '0')
    expect(preview).toHaveClass('overflow-auto', 'whitespace-pre', 'break-normal')
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Permission mode: Normal')
  })

  it('locks a double click and keeps a failed approval retryable', async () => {
    let reject!: (error: Error) => void
    const approve = vi.spyOn(api, 'approveChatSlot').mockReturnValue(new Promise((_resolve, no) => { reject = no }))
    renderWithProviders(<AttentionCard item={approval} title="Worker" />)
    const button = screen.getByRole('button', { name: 'Approve once' })
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(1))
    await act(async () => reject(new Error('Offline')))
    expect(await screen.findByText('Offline')).toBeInTheDocument()
    await waitFor(() => expect(button).toBeEnabled())
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
  })

  it('answers a blocking question by ID, never by sending to the active chat', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true })
    const send = vi.spyOn(transport, 'sendTurn')
    renderWithProviders(<AttentionCard item={question} title="Worker" />)
    expect(screen.getByRole('heading', { name: 'Worker' })).toBeVisible()
    expect(screen.queryByText('From session: Worker')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(answer).toHaveBeenCalledWith('q1', { 'Which scope?': 'Backend' }))
    expect(send).not.toHaveBeenCalled()
  })

  it('retires an expired approval without claiming it was approved or offering another submission', async () => {
    vi.spyOn(api, 'approveChatSlot').mockRejectedValue(new ApiError(404, 'expired'))
    renderWithProviders(<AttentionCard item={approval} title="Worker" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument())
    expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open session' })).toHaveAttribute('href', '/chat?sid=child')
  })

  it.each([false, true])('preserves the selected answer after an uncertain direct delivery (snapshot running=%s)', async running => {
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [{ key: 'child', messages: 0, running }] } })
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'unknown', body: {} })
    const dismiss = vi.spyOn(api, 'dismissQuestionCard')
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card', native: true } }} title="Worker" />, { store })
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await screen.findByText(/Delivery is uncertain/)
    expect(send).toHaveBeenCalledWith({ slot: 'child', message: 'Which scope?: Backend', ...(running ? { steer: true } : {}) })
    expect(dismiss).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    expect(send).toHaveBeenCalledTimes(1)
  })

  it.each([true, false])('steers only a native answer while its own slot is busy (native=%s)', async (native) => {
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial, chat: { ...initial.chat, activeSlot: 'child', slotRunning: true } })
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'queued', body: {} })
    vi.spyOn(api, 'dismissQuestionCard').mockResolvedValue({ ok: true })
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card', native } }} title="Worker" />, { store })
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await screen.findByText('Your response was recorded.')
    expect(send).toHaveBeenCalledWith({ slot: 'child', message: 'Which scope?: Backend', ...(native ? { steer: true } : {}) })
  })

  it.each([
    { native: true, running: true, siblingRunning: false, steer: true },
    { native: true, running: true, siblingRunning: true, steer: true },
    { native: true, running: false, siblingRunning: true, steer: false },
    { native: false, running: true, siblingRunning: true, steer: false },
  ])('uses only its owning reloaded slot snapshot: %j', async ({ native, running, siblingRunning, steer }) => {
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial,
      chat: { ...initial.chat, activeSlot: 'sibling', slotRunning: siblingRunning },
      dashboard: { ...initial.dashboard, slots: [
        { key: 'child', messages: 0, running }, { key: 'sibling', messages: 0, running: siblingRunning },
      ] },
    })
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'dispatched', body: {} })
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, native } }} title="Worker" />, { store })
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await screen.findByText('Your response was recorded.')
    expect(send).toHaveBeenCalledWith({ slot: 'child', message: 'Which scope?: Backend', ...(steer ? { steer: true } : {}) })
  })

  it('treats a card the answer already retired as done, not as an error', async () => {
    vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'dispatched', body: { ok: true } })
    const dismiss = vi.spyOn(api, 'dismissQuestionCard').mockRejectedValue(new ApiError(404, 'no pending question card for that slot and card_id'))
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card' } }} title="Worker" />)
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await screen.findByText('Your response was recorded.')
    await waitFor(() => expect(dismiss).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/no pending question card/)).not.toBeInTheDocument()
  })

  it('never sends the answer twice when retiring its already-delivered card fails', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'dispatched', body: { ok: true } })
    vi.spyOn(api, 'dismissQuestionCard').mockRejectedValue(new Error('Retirement failed'))
    renderWithProviders(<AttentionCard item={{ ...question, question: { ...question.question!, ask_id: undefined, card_id: 'card' } }} title="Worker" />)
    fireEvent.click(screen.getByText('Backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await screen.findByText('Retirement failed')
    expect(screen.queryByRole('button', { name: 'Send answer' })).not.toBeInTheDocument()
    expect(send).toHaveBeenCalledTimes(1)
  })
})
