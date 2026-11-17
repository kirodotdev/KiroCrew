/**
 * A crewmate's Sessions list: the mate's sessions in the Crew panel's groups, and a
 * row click handed to the host (`onOpen`), which opens the session in the side
 * panel. The recap is records-only, with no model-written summary.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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

import MateSessionsList, { MateSessionRecap, RecapBody } from './MateSessionsList'
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

function mount(live: Partial<ChatSlot>[] = [], onOpen: (w: CrewWorker) => void = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MateSessionsList root={MATE} mateName="Ada" liveSlots={live as ChatSlot[]} slotsLoaded
        empty={<p data-testid="empty">empty</p>} onOpen={onOpen} />
    </QueryClientProvider>,
  )
}

function mountRecap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MateSessionRecap root={MATE} worker="m1" rowTitle="Release notes" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  crewWorkers.mockReset()
  crewWorkerRecap.mockReset()
  sessionSummary.mockReset()
  generateSessionSummary.mockReset()
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
        <MateSessionsList root="" mateName="Ada" liveSlots={[]} slotsLoaded empty={<p data-testid="empty">empty</p>} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('empty')).toBeTruthy()
    expect(screen.queryByTestId('mate-sessions-loading')).toBeNull()
    expect(crewWorkers).not.toHaveBeenCalled()
  })

  it('hands a row click to the host, never navigates, and reads no recap', async () => {
    crewWorkers.mockResolvedValue({ root: MATE, workers: [worker('m1', { title: 'Release notes' })] })
    const onOpen = vi.fn()
    const before = window.location.href
    const pushState = vi.spyOn(window.history, 'pushState')
    mount([], onOpen)
    fireEvent.click(await screen.findByTestId('mate-session-open'))
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ key: 'm1' }))
    expect(screen.queryByTestId('mate-recap')).toBeNull()
    expect(crewWorkerRecap).not.toHaveBeenCalled()
    expect(window.location.href).toBe(before)
    expect(pushState).not.toHaveBeenCalled()
    pushState.mockRestore()
  })

  it('reads the recap from records only, as plain text behind a left rule', async () => {
    crewWorkerRecap.mockResolvedValue(RECAP)
    mountRecap()
    const recap = await screen.findByTestId('mate-recap')
    expect(crewWorkerRecap).toHaveBeenCalledWith(MATE, 'm1')
    expect(sessionSummary).not.toHaveBeenCalled()
    expect(generateSessionSummary).not.toHaveBeenCalled()
    const recapClasses = recap.className.split(/\s+/)
    expect(recapClasses.filter(c => /^(bg-|rounded)/.test(c))).toEqual([])
    expect(recapClasses).toContain('border-l-2')
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
    crewWorkerRecap.mockResolvedValueOnce(RECAP)
    mountRecap()
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
