/**
 * A crewmate's Sessions tab: the mate's sessions in the Crew panel's groups, and a
 * click that expands the row IN PLACE with a records-only recap
 * (rfc-mate-sessions-folder-or-lineage.md, "Owner decision: a click shows the recap
 * in place"). No navigation, no side panel, no model-written summary. The recap's
 * one action, Open session, leaves for the chat view after the page's
 * unsaved-edits question, reopening a closed session first.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { act, cleanup, render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'
import type { ChatSlot, CrewWorker, CrewWorkerRecap } from '../../types'

const crewWorkers = vi.fn()
const crewWorkerRecap = vi.fn()
const sessionSummary = vi.fn()
const generateSessionSummary = vi.fn()
vi.mock('../../api/client', () => ({
  api: {
    crewWorkers: (root: string) => crewWorkers(root),
    crewWorkerRecap: (root: string, worker: string) => crewWorkerRecap(root, worker),
    sessionSummary: (slot: string) => sessionSummary(slot),
    generateSessionSummary: (slot: string) => generateSessionSummary(slot),
  },
}))

const reopenCrewSession = vi.hoisted(() => vi.fn())
vi.mock('../chat/command-center/CrewWorkerList', async importOriginal => ({
  ...(await importOriginal<typeof import('../chat/command-center/CrewWorkerList')>()),
  reopenCrewSession,
}))

import MateSessionsList, { RecapBody } from './MateSessionsList'
import { ApiError } from '../../api/apiError'

const MATE = 'dm-ada'

function worker(key: string, over: Partial<CrewWorker> = {}): CrewWorker {
  return { key, title: key, agent: 'kirocrew', status: 'idle', group: 'waiting', last_activity: '', ledger_item: '', created_by: MATE, ...over }
}

const RECAP: CrewWorkerRecap = {
  key: 'm1', root: MATE, title: 'Write the release notes', ledger_item: 'it_1',
  status: 'blocked', reason: 'needs_human', state: 'open',
  summary: 'Draft done; waiting on the changelog owner.', decision: 'Ship the short version first.',
  pr: 42, pr_url: 'https://github.com/kirodotdev/KiroCrew/pull/42', artifacts: {},
  last_update: '2026-11-03T05:00:00Z',
  open_approvals: 1, approvals: [{ tool: 'Shell', requested_at: '' }], open_questions: 2, live: true,
}

const confirmLeave = vi.fn<() => Promise<boolean>>()
const openOnChatPage = vi.fn<(key: string) => void>()

function mount(live: Partial<ChatSlot>[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createTestStore({ dashboard: { slots: [] } } as never)}>
      <QueryClientProvider client={qc}>
        <MateSessionsList root={MATE} mateName="Ada" liveSlots={live as ChatSlot[]} slotsLoaded
          confirmLeave={confirmLeave} openOnChatPage={openOnChatPage}
          empty={<p data-testid="empty">empty</p>} />
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  crewWorkers.mockReset()
  crewWorkerRecap.mockReset()
  sessionSummary.mockReset()
  generateSessionSummary.mockReset()
  reopenCrewSession.mockReset()
  confirmLeave.mockReset().mockResolvedValue(true)
  openOnChatPage.mockReset()
})

describe('MateSessionsList', () => {
  it('groups the mate\'s sessions the way the Crew panel does and folds Done', async () => {
    crewWorkers.mockResolvedValue({
      root: MATE, root_kind: 'mate', workers: [
        worker('m1', { title: 'Release notes', group: 'working', status: 'working' }),
        worker('m2', { title: 'Check links', group: 'with_conductor' }),
        worker('m3', { title: 'Old task', group: 'done', status: 'closed' }),
      ],
    })
    mount()
    await screen.findByText('Release notes')
    expect(crewWorkers).toHaveBeenCalledWith(MATE)
    expect(screen.getByTestId('mate-sessions-group-working')).toBeTruthy()
    // "Waiting on the main chat" reads as waiting on the mate here.
    expect(screen.getByTestId('mate-sessions-group-with_conductor').textContent).toContain('Waiting on Ada')
    const done = screen.getByTestId('mate-sessions-done') as HTMLDetailsElement
    expect(done.open).toBe(false)
  })

  it('shows the open sessions from the store before, or without, the route', async () => {
    crewWorkers.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    mount([
      { key: 'g1', title: 'Grandchild', mate_root: MATE, created_by: 'm1', running: true },
      // Created by the mate, then adopted elsewhere: no longer stamped, so not listed.
      { key: 'moved', title: 'Adopted away', created_by: MATE, crew_root: 'croot' },
    ])
    expect(await screen.findByText('Grandchild')).toBeTruthy()
    expect(screen.queryByText('Adopted away')).toBeNull()
  })

  it('shows the empty state when the mate has no session', async () => {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [] })
    mount()
    expect(await screen.findByTestId('empty')).toBeTruthy()
  })

  it('shows the empty state, not a skeleton, while no root is known', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MateSessionsList root="" mateName="Ada" liveSlots={[]} slotsLoaded confirmLeave={confirmLeave}
          openOnChatPage={openOnChatPage} empty={<p data-testid="empty">empty</p>} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('empty')).toBeTruthy()
    expect(screen.queryByTestId('mate-sessions-loading')).toBeNull()
    expect(crewWorkers).not.toHaveBeenCalled()
  })

  it('expands a row in place, never navigates, and reads the recap only', async () => {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [worker('m1', { title: 'Release notes' })] })
    crewWorkerRecap.mockResolvedValue(RECAP)
    const before = window.location.href
    const pushState = vi.spyOn(window.history, 'pushState')
    mount()
    const toggle = await screen.findByTestId('mate-session-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    // The row says it opens details, so a click under "Approval needed" never reads as approving.
    expect(toggle.textContent).toContain('Details')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const recap = await screen.findByTestId('mate-recap')
    expect(crewWorkerRecap).toHaveBeenCalledWith(MATE, 'm1')
    // In place: inside the row, no route change, no history entry.
    expect(screen.getByTestId('mate-session-row').contains(recap)).toBe(true)
    expect(window.location.href).toBe(before)
    expect(pushState).not.toHaveBeenCalled()
    // Records only: neither summary route is read or generated.
    expect(sessionSummary).not.toHaveBeenCalled()
    expect(generateSessionSummary).not.toHaveBeenCalled()
    // Expanding is not opening: only the recap's button leaves the page.
    expect(confirmLeave).not.toHaveBeenCalled()
    expect(openOnChatPage).not.toHaveBeenCalled()
    // A second click folds it again.
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByTestId('mate-recap')).toBeNull())
    pushState.mockRestore()
  })

  it('shows the open recap as plain text behind a left rule, apart from its hovered header', async () => {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [worker('m1', { title: 'Release notes' })] })
    crewWorkerRecap.mockResolvedValue(RECAP)
    mount()
    const toggle = await screen.findByTestId('mate-session-toggle')
    fireEvent.click(toggle)
    const recap = await screen.findByTestId('mate-recap')
    // The recap sits under the toggle, which fills on hover: a fill or box of
    // its own would make the two read as one block, so it paints neither, and a
    // left rule marks it as this row's recap instead.
    expect(toggle.className.split(/\s+/).some(c => c.startsWith('hover:bg-'))).toBe(true)
    const recapClasses = recap.className.split(/\s+/)
    expect(recapClasses.filter(c => /^(bg-|rounded)/.test(c))).toEqual([])
    expect(recapClasses).toContain('border-l-2')
    // And a top gap, so the hovered header's edge never touches the text.
    expect(screen.getByRole('region', { name: 'Release notes' }).className.split(/\s+/)).toContain('pt-2')
  })
})

describe('MateSessionsList Open session', () => {
  async function openRecap(w: CrewWorker) {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [w] })
    crewWorkerRecap.mockResolvedValue(RECAP)
    mount()
    fireEvent.click(await screen.findByTestId('mate-session-toggle'))
    await screen.findByTestId('mate-recap')
  }

  it('opens a live session in the chat view after the unsaved-edits question', async () => {
    await openRecap(worker('m1', { title: 'Release notes' }))
    const open = screen.getByRole('button', { name: 'Open session: Release notes' })
    expect(open.textContent).toBe('Open session')
    expect(screen.getByRole('region', { name: 'Release notes' }).contains(open)).toBe(true)
    fireEvent.click(open)
    await waitFor(() => expect(openOnChatPage).toHaveBeenCalledWith('m1'))
    expect(confirmLeave).toHaveBeenCalledTimes(1)
    expect(reopenCrewSession).not.toHaveBeenCalled()
  })

  it('stays on the page when the person keeps their unsaved edits', async () => {
    confirmLeave.mockResolvedValue(false)
    await openRecap(worker('m3', { title: 'Old task', group: 'done', status: 'closed' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reopen session: Old task' }))
    await waitFor(() => expect(confirmLeave).toHaveBeenCalledTimes(1))
    expect(reopenCrewSession).not.toHaveBeenCalled()
    expect(openOnChatPage).not.toHaveBeenCalled()
  })

  it('reopens a closed session from its saved conversation, then opens it', async () => {
    let land: (r: { ok: boolean; activated: boolean; key: string }) => void = () => {}
    reopenCrewSession.mockImplementationOnce(() => new Promise(r => { land = r }))
    await openRecap(worker('m3', { title: 'Old task', group: 'done', status: 'closed' }))
    // The row's group does not say "closed", so the recap says it, and that a
    // reopen runs nothing, before the button that reads "Reopen".
    expect(screen.getByTestId('mate-session-closed-note').textContent).toContain('closed')
    const reopen = screen.getByRole('button', { name: 'Reopen session: Old task' })
    fireEvent.click(reopen)
    await waitFor(() => expect(reopenCrewSession).toHaveBeenCalledTimes(1))
    expect(reopenCrewSession.mock.calls[0][2]).toBe('m3')
    expect(reopenCrewSession.mock.calls[0][3]).toBe('Old task')
    // Lands as a live open does: no Workers panel held open beside it.
    expect(reopenCrewSession.mock.calls[0][4]).toBe(false)
    // One reopen at a time: the button holds, keeps focus, and says what it is doing.
    expect(reopen.getAttribute('aria-disabled')).toBe('true')
    expect(reopen).not.toBeDisabled()
    expect(reopen.getAttribute('aria-label')).toBe('Reopening Old task…')
    expect(reopen.textContent).toBe('Reopening…')
    fireEvent.click(reopen)
    await act(async () => {})
    expect(confirmLeave).toHaveBeenCalledTimes(1)
    expect(openOnChatPage).not.toHaveBeenCalled()
    await act(async () => { land({ ok: true, activated: true, key: 'm3-reopened' }) })
    expect(openOnChatPage).toHaveBeenCalledWith('m3-reopened')
  })

  it('stays on the page and says so when the reopen fails', async () => {
    reopenCrewSession.mockResolvedValueOnce({ ok: false, activated: false, key: 'm3' })
    await openRecap(worker('m3', { title: 'Old task', group: 'done', status: 'closed' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reopen session: Old task' }))
    expect((await screen.findByTestId('mate-session-reopen-failed')).textContent).toContain('Could not reopen that session. Try again.')
    expect(openOnChatPage).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Reopen session: Old task' }).getAttribute('aria-disabled')).toBeNull()
  })

  it('does not move the person when the reopen lands after they left the tab', async () => {
    let land: (r: { ok: boolean; activated: boolean; key: string }) => void = () => {}
    reopenCrewSession.mockImplementationOnce(() => new Promise(r => { land = r }))
    await openRecap(worker('m3', { title: 'Old task', group: 'done', status: 'closed' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reopen session: Old task' }))
    await waitFor(() => expect(reopenCrewSession).toHaveBeenCalledTimes(1))
    cleanup()
    await act(async () => { land({ ok: true, activated: true, key: 'm3' }) })
    expect(openOnChatPage).not.toHaveBeenCalled()
  })

  it('still opens under StrictMode, which re-runs effects in development', async () => {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [worker('m1', { title: 'Release notes' })] })
    crewWorkerRecap.mockResolvedValue(RECAP)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <StrictMode>
        <Provider store={createTestStore({ dashboard: { slots: [] } } as never)}>
          <QueryClientProvider client={qc}>
            <MateSessionsList root={MATE} mateName="Ada" liveSlots={[]} slotsLoaded
              confirmLeave={confirmLeave} openOnChatPage={openOnChatPage} empty={<p>empty</p>} />
          </QueryClientProvider>
        </Provider>
      </StrictMode>,
    )
    fireEvent.click(await screen.findByTestId('mate-session-toggle'))
    fireEvent.click(await screen.findByRole('button', { name: 'Open session: Release notes' }))
    await waitFor(() => expect(openOnChatPage).toHaveBeenCalledWith('m1'))
  })

  it('leaves the view alone when the person picked another session during the reopen', async () => {
    reopenCrewSession.mockResolvedValueOnce({ ok: true, activated: false, key: 'm3' })
    await openRecap(worker('m3', { title: 'Old task', group: 'done', status: 'closed' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reopen session: Old task' }))
    await waitFor(() => expect(reopenCrewSession).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(openOnChatPage).not.toHaveBeenCalled()
    expect(screen.queryByTestId('mate-session-reopen-failed')).toBeNull()
  })

  it('offers no button for a session on another crew and says why', async () => {
    await openRecap(worker('r1', { title: 'Remote task', remote_crew: 'Build farm' }))
    expect(screen.queryByTestId('mate-session-open')).toBeNull()
    expect(screen.getByTestId('mate-session-remote').textContent).toBe('This session runs on Build farm. Open it from that crew.')
  })
})

describe('MateSessionsList polling', () => {
  it('keeps reading a mate that is not a root yet, and stops on a route that is not served', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    crewWorkers.mockRejectedValue(new ApiError(404, 'not a root', '{"code":"not_crew_mode_root"}'))
    mount([{ key: 'g1', title: 'Grandchild', mate_root: MATE, created_by: 'm1' }])
    await screen.findByText('Grandchild')
    const first = crewWorkers.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_500)
    expect(crewWorkers.mock.calls.length).toBeGreaterThan(first)
  })
  it('keeps a loaded recap on screen and polling while the mate thread restores', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    crewWorkers.mockResolvedValue({ root: MATE, workers: [worker('m1', { title: 'Release notes' })] })
    crewWorkerRecap.mockResolvedValueOnce(RECAP)
    mount()
    fireEvent.click(await screen.findByTestId('mate-session-toggle'))
    await screen.findByTestId('mate-recap')
    crewWorkerRecap.mockRejectedValue(new ApiError(404, 'not a root', '{"code":"not_crew_mode_root"}'))
    await vi.advanceTimersByTimeAsync(10_500)
    await vi.advanceTimersByTimeAsync(10_500)
    const calls = crewWorkerRecap.mock.calls.length
    expect(screen.getByTestId('mate-recap')).toBeTruthy()
    await vi.advanceTimersByTimeAsync(10_500)
    expect(crewWorkerRecap.mock.calls.length).toBeGreaterThan(calls)
  })
  afterEach(() => vi.useRealTimers())
})

describe('RecapBody', () => {
  function body(recap: CrewWorkerRecap, rowTitle = 'Release notes') {
    const qc = new QueryClient()
    return render(<QueryClientProvider client={qc}><RecapBody recap={recap} rowTitle={rowTitle} /></QueryClientProvider>)
  }

  it('renders exactly the RFC table\'s lines', () => {
    body(RECAP)
    const r = screen.getByTestId('mate-recap')
    expect(within(r).getByTestId('mate-recap-title').textContent).toBe('Write the release notes')
    expect(r.textContent).toContain('Blocked · needs a person')
    expect(r.textContent).toContain('Draft done; waiting on the changelog owner.')
    expect(r.textContent).toContain('Ship the short version first.')
    const link = within(r).getByRole('link', { name: '#42' })
    expect(link.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/pull/42')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(r.textContent).toContain("Waiting for approval1 · open that session from the sidebar to answer it")
    expect(r.textContent).toContain("Open questions2 · open that session from the sidebar to answer it")
    expect(r.textContent).toContain('Updated')
    expect(within(r).queryByTestId('mate-recap-empty')).toBeNull()
  })

  it('labels a PR link by the PR it opens, never by a different number', () => {
    body({ ...RECAP, pr: 9, pr_url: 'https://github.com/o/n/pull/7' })
    const link = within(screen.getByTestId('mate-recap')).getByRole('link')
    expect(link.textContent).toBe('#7')
    expect(link.getAttribute('href')).toBe('https://github.com/o/n/pull/7')
  })

  it.each([
    'https://github.com/o/n/pull/7/other',
    'https://github.com//n/pull/7',
    'https://github.com/o/n/pull/7?x=1',
    'http://github.com/o/n/pull/7',
    'https://github.com.evil/o/n/pull/7',
    'https://user@github.com/o/n/pull/7',
    'https://github.com:8443/o/n/pull/7',
    'https://github.com/o/x/../n/pull/7',
    'https://github.com/o/n/pull/7/files/more',
    'https://github.com/o/n/pull/7/files',
    'https://github.com/o/n/pull/7/commits',
  ])('never labels %s as a pull request number', (url) => {
    body({ ...RECAP, pr: null, pr_url: url })
    expect(within(screen.getByTestId('mate-recap')).getByRole('link').textContent).toBe(url)
  })

  it('never labels a branch link as a pull request number', () => {
    body({ ...RECAP, pr: null, pr_url: 'https://github.com/o/n/tree/pull/7' })
    expect(within(screen.getByTestId('mate-recap')).getByRole('link').textContent).toBe('https://github.com/o/n/tree/pull/7')
  })

  it('says "No report yet" when the record holds nothing', () => {
    body({ ...RECAP, title: 'Release notes', ledger_item: '', status: '', reason: '', state: '', summary: '', decision: '', pr: null, pr_url: '', last_update: '', open_approvals: 0, approvals: [], open_questions: 0 })
    const r = screen.getByTestId('mate-recap')
    expect(within(r).getByTestId('mate-recap-empty').textContent).toBe('No report yet')
    // The title is the row's own, so it is not repeated.
    expect(within(r).queryByTestId('mate-recap-title')).toBeNull()
    expect(r.querySelector('dl')).toBeNull()
  })

  it('still shows open approvals when there is no report', () => {
    body({ ...RECAP, status: '', reason: '', state: '', summary: '', decision: '', pr: null, pr_url: '', last_update: '', open_questions: 0 })
    const r = screen.getByTestId('mate-recap')
    expect(within(r).getByTestId('mate-recap-empty')).toBeTruthy()
    expect(r.textContent).toContain('Waiting for approval')
  })
})
