/**
 * A crew member's approval notice outside the conductor's card, when that
 * conductor is CLOSED: its key is no live slot, so a plain link to it would find
 * no session. The notice reopens the conductor from its saved transcript first,
 * then goes to its card; a failed reopen says so and can be tried again.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from './helpers'
import type { AttentionItem } from '../pages/chat/command-center/model'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../api/client', () => ({ api }))
const reopenCrewSession = vi.hoisted(() => vi.fn())
vi.mock('../pages/chat/command-center/CrewWorkerList', async importOriginal => ({
  ...(await importOriginal<typeof import('../pages/chat/command-center/CrewWorkerList')>()),
  reopenCrewSession,
}))

import AttentionCard from '../pages/chat/command-center/AttentionCard'

type Reopened = { ok: boolean; activated: boolean; key: string }
const activated: Reopened = { ok: true, activated: true, key: 'root' }
const failed: Reopened = { ok: false, activated: false, key: 'root' }

const item: AttentionItem = {
  id: 'w1:rid-1', slot: 'w1', kind: 'approval', crewCard: 'root',
  approval: { request_id: 'rid-1', tool: 'Shell', tool_input: 'ls' } as AttentionItem['approval'],
}

function Where() {
  const loc = useLocation()
  return <span data-testid="where">{loc.pathname + loc.search}</span>
}

async function renderNotice(slots: unknown[], onOpenSession?: (slot: string) => void) {
  const store = createTestStore({ dashboard: { slots } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let view: ReturnType<typeof render> | undefined
  await act(async () => {
    view = render(<QueryClientProvider client={qc}><Provider store={store}><MemoryRouter initialEntries={['/dashboards']}>
      <Routes>
        <Route path="/dashboards" element={<AttentionCard item={item} title="Worker B" onOpenSession={onOpenSession} />} />
        <Route path="/chat" element={<Where />} />
      </Routes>
    </MemoryRouter></Provider></QueryClientProvider>)
  })
  return { store, qc, unmount: () => view!.unmount() }
}

describe('the crew approval notice when the conductor is closed', () => {
  afterEach(() => { vi.clearAllMocks() })

  it('reopens the conductor under its saved title, then opens its card', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Paper Flags', workers: [] })
    let land: (r: Reopened) => void = () => {}
    reopenCrewSession.mockImplementationOnce(() => new Promise<Reopened>(r => { land = r }))
    await renderNotice([{ key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true }])
    expect(await screen.findByText('Decide it on the approval card in Paper Flags.')).toBeInTheDocument()
    // No link to a key that is no live slot: a button that reopens it.
    expect(screen.queryByRole('link', { name: /Open the approval card/ })).toBeNull()
    const open = screen.getByTestId('crew-approval-reopen-root')
    fireEvent.click(open)
    expect(open).toBeDisabled()
    fireEvent.click(open)
    expect(reopenCrewSession).toHaveBeenCalledTimes(1)
    expect(reopenCrewSession.mock.calls[0][2]).toBe('root')
    expect(reopenCrewSession.mock.calls[0][3]).toBe('Paper Flags')
    await act(async () => { land(activated) })
    expect(screen.getByTestId('where')).toHaveTextContent('/chat?sid=root')
  })

  it('takes the host\'s guarded exit after the reopen when the host gives one', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Paper Flags', workers: [] })
    reopenCrewSession.mockResolvedValueOnce(activated)
    const onOpenSession = vi.fn()
    await renderNotice([{ key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true }], onOpenSession)
    await act(async () => { fireEvent.click(screen.getByTestId('crew-approval-reopen-root')) })
    expect(onOpenSession).toHaveBeenCalledWith('root')
  })

  it('says a failed reopen, stays put, and clears the notice on a retry', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Paper Flags', workers: [] })
    reopenCrewSession.mockResolvedValueOnce(failed)
    await renderNotice([{ key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true }])
    await act(async () => { fireEvent.click(screen.getByTestId('crew-approval-reopen-root')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reopen that session. Try again.')
    expect(screen.queryByTestId('where')).toBeNull()
    let land: (r: Reopened) => void = () => {}
    reopenCrewSession.mockImplementationOnce(() => new Promise<Reopened>(r => { land = r }))
    fireEvent.click(screen.getByTestId('crew-approval-reopen-root'))
    expect(screen.queryByRole('alert')).toBeNull()
    await act(async () => { land(activated) })
  })

  it('does not pull the person back when the reopen lands after they left the page', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Paper Flags', workers: [] })
    let land: (r: Reopened) => void = () => {}
    reopenCrewSession.mockImplementationOnce(() => new Promise<Reopened>(r => { land = r }))
    const onOpenSession = vi.fn()
    const { unmount } = await renderNotice([{ key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true }], onOpenSession)
    fireEvent.click(screen.getByTestId('crew-approval-reopen-root'))
    // The person goes elsewhere (Settings, say): the notice unmounts.
    unmount()
    await act(async () => { land(activated) })
    expect(onOpenSession).not.toHaveBeenCalled()
  })

  it('stays where the person went when they picked another session while it reopened', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Paper Flags', workers: [] })
    reopenCrewSession.mockResolvedValueOnce({ ok: true, activated: false, key: 'root' })
    await renderNotice([{ key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true }])
    await act(async () => { fireEvent.click(screen.getByTestId('crew-approval-reopen-root')) })
    // Reopened as a tab, but the newer pick wins: no navigation, and no error.
    expect(screen.queryByTestId('where')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps the plain link while the conductor is open', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderNotice([
      { key: 'root', title: 'Paper Flags', crew_mode_root: true },
      { key: 'w1', title: 'Worker B', crew_root: 'root', pending_approval: true },
    ])
    expect(screen.getByRole('link', { name: /Open the approval card/ })).toHaveAttribute('href', '/chat?sid=root')
    expect(screen.queryByTestId('crew-approval-reopen-root')).toBeNull()
    expect(reopenCrewSession).not.toHaveBeenCalled()
  })
})
