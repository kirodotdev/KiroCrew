/**
 * The hooks table's Actions cell: a primary Test button, an arm→Confirm Delete,
 * and a ⋯ overflow menu holding the rest (the CronRowActions convention,
 * `max-two-buttons-per-row`).
 *
 * What this pins, and why each half matters:
 *  - the row carries exactly Test + Delete + the ⋯ trigger, with Edit in the
 *    overflow — three peer action buttons violate the max-two-buttons-per-row
 *    rule (two controls plus an overflow trigger is the rule's own "Good"
 *    shape), and the collapse is the prerequisite for pinning this column
 *    sticky-right (#4296) without the pin devouring a narrow viewport;
 *  - Delete is a two-click arm→Confirm state machine, not a window.confirm():
 *    the first click must arm (label changes, nothing deleted), the second must
 *    delete, and the armed state must decay back to disarmed on its own so an
 *    accidental first click cannot leave a live destructive button on the row.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const deleteHook = vi.fn().mockResolvedValue({})
const testHook = vi.fn().mockResolvedValue({ result: { exit_code: 0, duration_ms: 1 } })
let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      if (prop === 'deleteHook') return deleteHook
      if (prop === 'testHook') return testHook
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

const HOOK = {
  id: 'h1', name: 'backup', event: 'Stop', matcher: '', matcher_mode: 'glob',
  command: 'true', skills: [], timeout: 30, enabled: true,
  last_run: 0, last_status: '', run_count: 0,
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The rendered hook row (scopes queries away from the New Hook / form area). */
async function findRow() {
  const name = await screen.findByText('backup')
  return within(name.closest('tr')!)
}

beforeEach(() => {
  vi.clearAllMocks()
  hooksPayload = { hooks: [{ ...HOOK }] }
})

afterEach(() => vi.useRealTimers())

describe('hooks table — row actions', () => {
  it('the row holds Test, Delete, and the ⋯ trigger; Edit is in the overflow', async () => {
    renderPage()
    const row = await findRow()

    expect(row.getByRole('button', { name: 'Test' })).toBeTruthy()
    expect(row.getByRole('button', { name: 'Delete' })).toBeTruthy()
    expect(row.getByRole('button', { name: 'More actions' })).toBeTruthy()
    // No inline Edit button — it lives in the menu.
    expect(row.queryByRole('button', { name: 'Edit' })).toBeNull()

    // Radix DropdownMenuTrigger opens on keyboard activation (Enter) in jsdom.
    fireEvent.keyDown(row.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    const items = await screen.findAllByRole('menuitem')
    expect(items.map(i => i.textContent)).toEqual(['Test', 'Edit'])
    // Delete deliberately stays OUT of the menu: a menu that closes on select
    // cannot host the arm→Confirm state.
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).toBeNull()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit' }))
    // The edit form opens for the row's hook.
    expect(await screen.findByText('Edit Hook')).toBeTruthy()
  })

  it('delete arms on the first click and deletes only on the second', async () => {
    renderPage()
    const row = await findRow()

    const del = row.getByRole('button', { name: 'Delete' })
    fireEvent.click(del)
    // Armed: the label states the pending action, nothing deleted yet.
    expect(deleteHook).not.toHaveBeenCalled()
    expect(row.getByRole('button', { name: 'Delete?' })).toBeTruthy()

    fireEvent.click(row.getByRole('button', { name: 'Delete?' }))
    await waitFor(() => expect(deleteHook).toHaveBeenCalledTimes(1))
    expect(deleteHook).toHaveBeenCalledWith('h1')
  })

  it('an armed delete decays back to disarmed instead of waiting forever', async () => {
    vi.useFakeTimers()
    renderPage()
    // findBy* under fake timers: flush the initial query microtasks manually.
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const row = within(screen.getByText('backup').closest('tr')!)

    fireEvent.click(row.getByRole('button', { name: 'Delete' }))
    expect(row.getByRole('button', { name: 'Delete?' })).toBeTruthy()

    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(row.queryByRole('button', { name: 'Delete?' })).toBeNull()
    expect(row.getByRole('button', { name: 'Delete' })).toBeTruthy()
    expect(deleteHook).not.toHaveBeenCalled()
  })

  it('testing from the menu and from the row both hit the same endpoint', async () => {
    renderPage()
    const row = await findRow()

    fireEvent.click(row.getByRole('button', { name: 'Test' }))
    await waitFor(() => expect(testHook).toHaveBeenCalledTimes(1))

    fireEvent.keyDown(row.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Test' }))
    await waitFor(() => expect(testHook).toHaveBeenCalledTimes(2))
    expect(testHook).toHaveBeenLastCalledWith('h1')
  })

  it('while the delete is in flight the button is disabled and shows progress', async () => {
    let resolveDelete!: (v: unknown) => void
    deleteHook.mockImplementationOnce(() => new Promise(r => { resolveDelete = r }))
    renderPage()
    const row = await findRow()

    fireEvent.click(row.getByRole('button', { name: 'Delete' }))
    fireEvent.click(row.getByRole('button', { name: 'Delete?' }))

    // In flight: no re-entrant second delete, and the label states progress.
    const inFlight = await row.findByRole('button', { name: '...' })
    expect((inFlight as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(inFlight)
    expect(deleteHook).toHaveBeenCalledTimes(1)

    resolveDelete({})
    await waitFor(() => expect(row.getByRole('button', { name: 'Delete' })).toBeTruthy())
  })

  it('arming a second row disarms the first — only one row may be armed', async () => {
    hooksPayload = { hooks: [{ ...HOOK }, { ...HOOK, id: 'h2', name: 'notify' }] }
    renderPage()
    const rowA = await findRow()
    const rowB = within(screen.getByText('notify').closest('tr')!)

    fireEvent.click(rowA.getByRole('button', { name: 'Delete' }))
    expect(rowA.getByRole('button', { name: 'Delete?' })).toBeTruthy()

    fireEvent.click(rowB.getByRole('button', { name: 'Delete' }))
    // The armed state moved: row B is armed, row A reverted to disarmed.
    expect(rowB.getByRole('button', { name: 'Delete?' })).toBeTruthy()
    expect(rowA.queryByRole('button', { name: 'Delete?' })).toBeNull()

    // Confirming on row B deletes row B's hook, never row A's.
    fireEvent.click(rowB.getByRole('button', { name: 'Delete?' }))
    await waitFor(() => expect(deleteHook).toHaveBeenCalledTimes(1))
    expect(deleteHook).toHaveBeenCalledWith('h2')
  })

  it('a second in-flight delete does not re-enable the first still-pending row', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    deleteHook.mockImplementation(() => new Promise(r => { resolvers.push(r) }))
    hooksPayload = { hooks: [{ ...HOOK }, { ...HOOK, id: 'h2', name: 'notify' }] }
    renderPage()
    const rowA = await findRow()
    const rowB = within(screen.getByText('notify').closest('tr')!)

    // Row A: arm and confirm — its delete is now pending.
    fireEvent.click(rowA.getByRole('button', { name: 'Delete' }))
    fireEvent.click(rowA.getByRole('button', { name: 'Delete?' }))
    await rowA.findByRole('button', { name: '...' })

    // Row B: arm and confirm while A is still in flight.
    fireEvent.click(rowB.getByRole('button', { name: 'Delete' }))
    fireEvent.click(rowB.getByRole('button', { name: 'Delete?' }))
    await rowB.findByRole('button', { name: '...' })

    // Row A must STILL be disabled: deriving the pending row from the
    // mutation's latest variables would re-enable it here and let a re-click
    // fire a duplicate delete for h1.
    const aBtn = rowA.getByRole('button', { name: '...' }) as HTMLButtonElement
    expect(aBtn.disabled).toBe(true)
    fireEvent.click(aBtn)
    expect(deleteHook).toHaveBeenCalledTimes(2)
    expect(deleteHook).toHaveBeenNthCalledWith(1, 'h1')
    expect(deleteHook).toHaveBeenNthCalledWith(2, 'h2')

    resolvers.forEach(r => r({}))
    await waitFor(() => expect(rowA.queryByRole('button', { name: '...' })).toBeNull())
    deleteHook.mockReset()
    deleteHook.mockResolvedValue({})
  })

  it('a settling delete disarms only its own row — an armed sibling stays armed', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    deleteHook.mockImplementation(() => new Promise(r => { resolvers.push(r) }))
    hooksPayload = { hooks: [{ ...HOOK }, { ...HOOK, id: 'h2', name: 'notify' }] }
    renderPage()
    const rowA = await findRow()
    const rowB = within(screen.getByText('notify').closest('tr')!)

    // Row A: arm and confirm — its delete is now pending.
    fireEvent.click(rowA.getByRole('button', { name: 'Delete' }))
    fireEvent.click(rowA.getByRole('button', { name: 'Delete?' }))
    await rowA.findByRole('button', { name: '...' })

    // Row B: arm (but do NOT confirm) while A is still in flight.
    fireEvent.click(rowB.getByRole('button', { name: 'Delete' }))
    expect(rowB.getByRole('button', { name: 'Delete?' })).toBeTruthy()

    // A settles. Its cleanup must disarm only its own id: an unconditional
    // setConfirmDeleteId(null) here silently swallowed B's armed state, so
    // the user's intended confirmation click merely re-armed B.
    resolvers.forEach(r => r({}))
    await waitFor(() => expect(rowA.queryByRole('button', { name: '...' })).toBeNull())
    expect(rowB.getByRole('button', { name: 'Delete?' })).toBeTruthy()

    // B's second click still deletes.
    fireEvent.click(rowB.getByRole('button', { name: 'Delete?' }))
    await waitFor(() => expect(deleteHook).toHaveBeenCalledTimes(2))
    expect(deleteHook).toHaveBeenNthCalledWith(2, 'h2')

    deleteHook.mockReset()
    deleteHook.mockResolvedValue({})
  })

  it('a failed delete disarms and re-enables the row, and surfaces the error', async () => {
    deleteHook.mockRejectedValueOnce(new Error('gateway said no'))
    renderPage()
    const row = await findRow()

    fireEvent.click(row.getByRole('button', { name: 'Delete' }))
    fireEvent.click(row.getByRole('button', { name: 'Delete?' }))

    // The reference page shipped exactly this bug — confirmDeleteId not
    // resetting on a failed delete (SchedulePage.test.tsx names it) — so pin
    // the recovery: the row comes back disarmed and clickable, and the
    // failure is surfaced rather than swallowed.
    await waitFor(() => expect(row.getByRole('button', { name: 'Delete' })).toBeTruthy())
    expect((row.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false)
    expect(row.queryByRole('button', { name: 'Delete?' })).toBeNull()
    expect(screen.getByText(/gateway said no/)).toBeTruthy()
  })
})
