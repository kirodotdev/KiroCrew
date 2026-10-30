import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'
import { updateSlot } from '../../store/dashboardSlice'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), crewWorkerApprove: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))

import { ApiError } from '../../api/apiError'
import CrewApprovalCards, { crewApprovalRequests } from './CrewApprovalCards'

/** A worker slot row waiting on a tool approval, as the live slot frame carries it. */
const waiting = (key: string, extra: Record<string, unknown> = {}) => ({
  key,
  title: `Worker ${key}`,
  crew_root: 'root',
  pending_approval: true,
  pending_approval_info: {
    origin: 'native', request_id: `rid-${key}`, request_mid: `mid-${key}`,
    tool: 'Shell', tool_input: `ls ${key}`, tool_kind: 'execute', tool_purpose: 'list files',
    trust_grantable: '1',
  },
  ...extra,
})

/** Mount the stack and let its worker-list read settle inside act, so no state
 *  update lands after a test has ended (edge review R2 F8: no act() warnings). */
async function renderCards(slots: unknown[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  await act(async () => {
    render(<QueryClientProvider client={qc}><Provider store={store}><CrewApprovalCards root="root" /></Provider></QueryClientProvider>)
  })
  return store
}

/** Let a decision's mutation and its list re-read settle inside act. */
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

describe('CrewApprovalCards (owner decision B)', () => {
  afterEach(() => vi.clearAllMocks())

  it('shows one card per waiting worker request: worker, task, exact tool, three choices', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [{ key: 'w1', summary: 'Fix the build', group: 'approval' }] })
    await renderCards([{ key: 'root', crew_mode_root: true }, waiting('w1'), { key: 'plain', pending_approval: true }])
    const card = screen.getByTestId('crew-approval-card')
    expect(within(card).getByRole('heading')).toHaveTextContent('Worker w1 needs approval')
    expect(card).toHaveTextContent('Shell')
    expect(within(card).getByRole('region', { name: 'The exact tool input' })).toHaveTextContent('ls w1')
    expect(await within(card).findByText('Fix the build')).toBeInTheDocument()
    for (const name of ['Approve', 'Reject', 'Trust this crew member']) {
      expect(within(card).getByRole('button', { name })).toBeInTheDocument()
    }
    // Trust reaches past this request: its scope is written beside it, not only
    // in a tooltip, and the button is described by that line.
    const trust = within(card).getByRole('button', { name: 'Trust this crew member' })
    const scope = within(card).getByText('Approves this and lets this crew member run tools without asking until Kiro Crew restarts.')
    expect(trust).toHaveAttribute('aria-describedby', scope.id)
    expect(within(screen.getByTestId('crew-approval-trust-row')).getAllByRole('button')).toHaveLength(1)
    // Another root's worker, and a plain session, get no card here.
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
  })

  it("keeps each card's task line from the last read when a re-read fails, and says the read failed", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ dashboard: { slots: [{ key: 'root', crew_mode_root: true }, waiting('w1')] } } as never)
    api.crewWorkers.mockResolvedValueOnce({ root: 'root', workers: [{ key: 'w1', summary: 'Fix the build', group: 'approval' }] })
    await act(async () => {
      render(<QueryClientProvider client={qc}><Provider store={store}><CrewApprovalCards root="root" /></Provider></QueryClientProvider>)
    })
    const card = screen.getByTestId('crew-approval-card')
    expect(await within(card).findByText('Fix the build')).toBeInTheDocument()
    api.crewWorkers.mockRejectedValueOnce(new Error('down'))
    await act(async () => { await qc.refetchQueries({ queryKey: ['crew-workers', 'root'] }) })
    expect(await screen.findByTestId('crew-approval-workers-error')).toBeInTheDocument()
    expect(within(screen.getByTestId('crew-approval-card')).getByText('Fix the build')).toBeInTheDocument()
    qc.clear()
  })

  it.each([
    ['Approve', 'approve'],
    ['Reject', 'reject'],
    ['Trust this crew member', 'trust'],
  ] as const)('%s decides on the worker slot through the crew route', async (label, action) => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: label }))
    await waitFor(() => expect(api.crewWorkerApprove).toHaveBeenCalledWith('root', 'w1', action, 'rid-w1', 'mid-w1', ''))
    await waitFor(() => expect(screen.queryByTestId('crew-approval-card')).toBeNull())
    await settle()
  })

  it('pins a coordinator request to its record instance (edge review R2 F7)', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    const row = waiting('w1')
    await renderCards([{ ...row, pending_approval_info: { origin: 'coordinator', request_id: 'spawn:abc', request_instance: 'a1b2c3', tool: 'spawn_run', tool_input: '', tool_kind: 'spawn' } }])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.crewWorkerApprove).toHaveBeenCalledWith('root', 'w1', 'approve', 'spawn:abc', '', 'a1b2c3'))
    await settle()
  })

  it('offers Trust only when the request is trust-grantable', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const row = waiting('w1')
    await renderCards([{ ...row, pending_approval_info: { ...row.pending_approval_info, trust_grantable: undefined } }])
    expect(screen.queryByRole('button', { name: 'Trust this crew member' })).toBeNull()
  })

  it('leaves the stack live when the request is decided elsewhere', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const store = await renderCards([waiting('w1'), waiting('w2')])
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(2)
    act(() => { store.dispatch(updateSlot({ key: 'w1', pending_approval: false, pending_approval_info: null } as never)) })
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
  })

  it('says the decision may not have landed on a transport failure, and keeps the buttons', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockRejectedValue(new TypeError('Failed to fetch'))
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByTestId('crew-approval-failure')).toHaveTextContent('This decision may not have been recorded — the request failed.')
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
    await settle()
  })

  it("quotes the server's own refusal, and keeps the buttons", async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockRejectedValue(new ApiError(409, "that worker's tab is closed"))
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(await screen.findByTestId('crew-approval-failure')).toHaveTextContent("This decision wasn't recorded: that worker's tab is closed")
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
    await settle()
  })

  it('says the request is already decided on a 404, and takes the buttons away', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockRejectedValue(new ApiError(404, 'no pending approval'))
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByTestId('crew-approval-failure')).toHaveTextContent('This approval has expired or was already decided')
    for (const name of ['Approve', 'Reject', 'Trust this crew member']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
    await settle()
  })

  it('gives a replacement request under the same id a fresh card after a decision', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    const row = waiting('w1')
    const store = await renderCards([row])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.queryByTestId('crew-approval-card')).toBeNull())
    await settle()
    // The same request id comes back on a new delivery, with no non-pending frame between.
    act(() => { store.dispatch(updateSlot({ key: 'w1', pending_approval: true, pending_approval_info: { ...row.pending_approval_info, request_mid: 'mid-w1-next' } } as never)) })
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
  })

  it('gives a replacement request under the same id usable buttons after a terminal refusal', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockRejectedValue(new ApiError(404, 'no pending approval'))
    const row = waiting('w1')
    const store = await renderCards([{ ...row, pending_approval_info: { origin: 'coordinator', request_id: 'spawn:abc', request_instance: 'i1', tool: 'spawn_run', tool_input: '', tool_kind: 'spawn' } }])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled())
    await settle()
    act(() => { store.dispatch(updateSlot({ key: 'w1', pending_approval: true, pending_approval_info: { origin: 'coordinator', request_id: 'spawn:abc', request_instance: 'i2', tool: 'spawn_run', tool_input: '', tool_kind: 'spawn' } } as never)) })
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
    expect(screen.queryByTestId('crew-approval-failure')).toBeNull()
  })

  it('stacks several requests oldest first and shows a long input whole', async () => {
    // Approve authorizes the whole input, so no part of it is hidden from the card.
    const long = 'x'.repeat(2050)
    const first = crewApprovalRequests([waiting('a')] as never, 'root', new Map(), 1000)
    expect(first.map(r => r.worker)).toEqual(['a'])
    const both = crewApprovalRequests([waiting('b'), waiting('a')] as never, 'root', new Map(), 2000)
    expect(both.map(r => r.worker)).toEqual(['a', 'b'])
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const row = waiting('c')
    await renderCards([{ ...row, pending_approval_info: { ...row.pending_approval_info, tool_input: long } }])
    const pre = screen.getByRole('region', { name: 'The exact tool input' })
    expect(pre.textContent).toBe(long)
  })

  it('says so when the worker list cannot be read, and keeps the cards decidable', async () => {
    api.crewWorkers.mockRejectedValue(new Error('500'))
    await renderCards([waiting('w1')])
    expect(await screen.findByTestId('crew-approval-workers-error')).toHaveTextContent('Could not load the crew.')
    // The exact tool input and the decisions do not depend on the list.
    expect(screen.getByRole('region', { name: 'The exact tool input' })).toHaveTextContent('ls w1')
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
  })

  it('renders nothing when no worker waits', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderCards([{ key: 'root', crew_mode_root: true }])
    expect(screen.queryByTestId('crew-approval-cards')).toBeNull()
  })
})
