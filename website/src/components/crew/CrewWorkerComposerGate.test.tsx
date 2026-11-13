import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))
// Reopening a closed conductor goes through the shared helper; the rest of the
// module (the worker summary the gate reads) stays real.
const reopenCrewWorker = vi.hoisted(() => vi.fn())
vi.mock('../../pages/chat/command-center/CrewWorkerList', async importOriginal => ({
  ...(await importOriginal<typeof import('../../pages/chat/command-center/CrewWorkerList')>()),
  reopenCrewWorker,
}))

import { CrewSteerUnrecordedNotice, CrewWorkerComposerGate, steerUnrecordedLine } from './CrewWorkerComposerGate'
import { crewRootOf } from '../../lib/crewMode'
import { PREVIEW_CREW_MODE } from '../../utils/previewFlags'
import { clearCrewSteerNotice, noteCrewSteerReceipt } from '../../lib/crewSteerNotice'

async function renderGate(slots: unknown[], waitingForApproval = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}><Provider store={store}>
        <CrewWorkerComposerGate root="root" worker="w1" workerTitle="Worker B" waitingForApproval={waitingForApproval} />
      </Provider></QueryClientProvider>,
    )
  })
  return store
}

const live = [{ key: 'root', title: 'Sample goal', crew_mode_root: true }, { key: 'w1', title: 'Worker B', crew_root: 'root' }]

describe('CrewWorkerComposerGate (edge review R2)', () => {
  afterEach(() => { vi.clearAllMocks(); clearCrewSteerNotice('w1') })

  it('F6: says when a steer was delivered but not recorded, and not otherwise', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: null, ledger_error: 'crew_log_off' }) })
    expect(screen.getByTestId('crew-steer-not-recorded')).toHaveTextContent('Delivered to Worker B, but not recorded on the ledger: the crew log is off.')
    // A later recorded steer clears it; a plain send (no ledger fields) leaves it alone.
    act(() => { noteCrewSteerReceipt('w1', { ok: true }) })
    expect(screen.getByTestId('crew-steer-not-recorded')).toBeInTheDocument()
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: 'it_1', ledger_error: null }) })
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
  })

  it('G7: each ledger code reads as a sentence, never the raw code', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: null, ledger_error: 'no_item' }) })
    const line = screen.getByTestId('crew-steer-not-recorded')
    expect(line).toHaveTextContent('Delivered to Worker B, but not recorded on the ledger: the crew member has no open task.')
    expect(line.textContent).not.toContain('no_item')
    expect(steerUnrecordedLine('crew_log_unit_unknown', 'W')).toBe('Delivered to W, but not recorded on the ledger: the ledger write failed.')
    expect(steerUnrecordedLine('not_recorded', 'W')).toBe('Delivered to W, but not recorded on the ledger: the ledger write failed.')
    expect(steerUnrecordedLine('item_closed', 'W')).toBe('Delivered. The task is already closed, so nothing was recorded.')
    expect(steerUnrecordedLine('some_new_code', 'W')).toBe('Delivered to W, but not recorded on the ledger.')
    act(() => { clearCrewSteerNotice('w1') })
  })

  it('F6: a queued steer (recorded at delivery) is not called unrecorded', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    act(() => { noteCrewSteerReceipt('w1', { ok: true, queued: true, ledger_event: null, ledger_error: null, ledger_pending: true }) })
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
  })

  it('Y1: names the conductor where the approval is decided, with a link to it', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const store = await renderGate(live, true)
    expect(screen.getByTestId('crew-worker-approval-waiting')).toHaveTextContent('Waiting for approval in Sample goal')
    fireEvent.click(screen.getByTestId('crew-worker-open-root'))
    expect(store.getState().chat.activeSlot).toBe('root')
  })

  it('shows a failed crew read with the reopen line when the conductor is closed', async () => {
    api.crewWorkers.mockRejectedValue(new Error('down'))
    await renderGate([{ key: 'w1', title: 'Worker B', crew_root: 'root' }], true)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the crew.')
    expect(screen.getByTestId('crew-worker-root-closed')).toBeInTheDocument()
    expect(screen.getByTestId('crew-worker-reopen-root')).toHaveTextContent('Open main chat')
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
  })

  it('Y2: Open main chat reopens the closed conductor once, under its saved title', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Toy Flag Review', workers: [] })
    let land: (ok: boolean) => void = () => {}
    reopenCrewWorker.mockImplementationOnce(() => new Promise<boolean>(r => { land = r }))
    await renderGate([{ key: 'w1', title: 'Worker B', crew_root: 'root' }])
    const button = screen.getByTestId('crew-worker-reopen-root')
    await screen.findByText('Main chat is closed. Open it again to message Worker B.')
    fireEvent.click(button)
    // In flight: the button is off, so a second click starts no second reopen.
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(reopenCrewWorker).toHaveBeenCalledTimes(1)
    expect(reopenCrewWorker.mock.calls[0][2]).toBe('root')
    expect(reopenCrewWorker.mock.calls[0][3]).toBe('Toy Flag Review')
    await act(async () => { land(true) })
    expect(button).not.toBeDisabled()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('Y2: a reopen that fails says so, and a later try clears the notice', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Toy Flag Review', workers: [] })
    reopenCrewWorker.mockResolvedValueOnce(false)
    await renderGate([{ key: 'w1', title: 'Worker B', crew_root: 'root' }])
    await screen.findByText('Main chat is closed. Open it again to message Worker B.')
    await act(async () => { fireEvent.click(screen.getByTestId('crew-worker-reopen-root')) })
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toBe('')
    let land: (ok: boolean) => void = () => {}
    reopenCrewWorker.mockImplementationOnce(() => new Promise<boolean>(r => { land = r }))
    fireEvent.click(screen.getByTestId('crew-worker-reopen-root'))
    expect(screen.queryByRole('alert')).toBeNull()
    await act(async () => { land(true) })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a steer to a closed task is a muted status line, dismissed with its ×', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: null, ledger_error: 'item_closed' }) })
    const line = screen.getByTestId('crew-steer-not-recorded')
    expect(line.tagName).toBe('P')
    expect(line).toHaveTextContent('Delivered. The task is already closed, so nothing was recorded.')
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
  })

  it('Y2: with the conductor closed it says the main chat is closed and whom opening it lets you message', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Toy Flag Review', workers: [] })
    await renderGate([{ key: 'w1', title: 'Worker B', crew_root: 'root' }], true)
    expect(await screen.findByText('Main chat is closed. Open it again to message Worker B.')).toBeInTheDocument()
    expect(screen.getByTestId('crew-worker-reopen-root')).toHaveTextContent('Open main chat')
    // The approval line gives way: nothing can be decided until the conductor is back.
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
  })
})

describe('CrewSteerUnrecordedNotice with the Crew Mode preview off', () => {
  afterEach(() => { act(() => { clearCrewSteerNotice('w1') }); localStorage.removeItem(PREVIEW_CREW_MODE) })

  it('a crew member\'s unrecorded steer still shows as an ErrorNotice when the gate is not mounted', async () => {
    localStorage.removeItem(PREVIEW_CREW_MODE)
    const worker = { key: 'w1', title: 'Worker B', crew_root: 'root' }
    // Preview off: the hosts read no crew root, so they mount no gate (no Crew Mode chrome).
    expect(crewRootOf(worker)).toBe('')
    const store = createTestStore({ dashboard: { slots: [worker] } } as never)
    await act(async () => {
      render(<Provider store={store}><CrewSteerUnrecordedNotice worker="w1" workerTitle="Worker B" /></Provider>)
    })
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: null, ledger_error: 'not_recorded' }) })
    const notice = screen.getByTestId('crew-steer-not-recorded')
    expect(notice.querySelector('[role="alert"]')).not.toBeNull()
    expect(notice).toHaveTextContent(steerUnrecordedLine('not_recorded', 'Worker B'))
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
    expect(screen.queryByTestId('crew-worker-root-closed')).toBeNull()
  })
})
