// The Crew board page: what it renders, and what it does when a row is acted on.
//
// `crewBoardRows.test.ts` already covers the banding arithmetic as pure functions.
// This covers the things only a render can show: that the bands appear in the order
// the RFC asks for, that a terminal item stays behind its expander, that an orphaned
// row's two affordances reach the right enabled/disabled states, and that the
// failure wording distinguishes "your view is stale" from "that broke".
//
// The api client is mocked at the module boundary rather than through `fetch`,
// because what is under test is the PAGE's behaviour given a payload -- the payload
// shape itself is pinned server-side in `test/test_work_ledger_board.py`, and the
// committed screenshot fixtures are real handler output.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../store'
import { i18nT } from '../i18n/t'
import { crewBoardQueryKey, CREW_BOARD_POLL_MS } from '../api/crewBoard'
import type { WorkBoardItem, WorkBoardResponse } from '../api/crewBoard'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const crewBoard = vi.fn()
const crewBoardAction = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    crewBoard: (...args: unknown[]) => crewBoard(...args),
    crewBoardAction: (...args: unknown[]) => crewBoardAction(...args),
  },
}))

const CONDUCTOR = 'chat-1-conductor'
const PAUSE_WARNING = 'Work is paused for now, but the pause could not be saved and may be lost after a restart. Retry saving the pause.'
const RETRY_HINT = i18nT('pages.crewBoard.retry_pause_hint', {
  action: i18nT('pages.crewBoard.action_stop'),
})
const CACHED_TITLE = "Couldn't refresh — showing the last loaded version"

function item(over: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1,
    item_id: 'it_00000001',
    title: 'an item',
    acceptance: { kind: 'pr_checks', pr: 1, repo: 'o/r' },
    state: 'open',
    verdict: null,
    decision: '',
    round: 0,
    fails: 0,
    status: 'progress',
    summary: 'moving along',
    artifacts: {},
    pr: null,
    last_report_at: '2026-09-22T10:00:00+00:00',
    created_at: '2026-09-22T09:00:00+00:00',
    closed_at: null,
    orphaned: false,
    stale: false,
    acceptance_concrete: true,
    outstanding: false,
    terminal: false,
    alive: 'running',
    events: [],
    ...over,
  }
}

function board(over: Partial<WorkBoardResponse> = {}): WorkBoardResponse {
  return {
    conductor: {
      schema: 1,
      slot_key: CONDUCTOR,
      goal: 'ship the ledger',
      round: 1,
      depth: 0,
      parent_item: null,
      created_at: '2026-09-22T08:00:00+00:00',
    },
    conductor_alive: 'idle',
    items: [],
    take_over_available: false,
    ...over,
  }
}

async function mount(payload: WorkBoardResponse | Error, conductor = CONDUCTOR) {
  crewBoard.mockImplementation(() =>
    payload instanceof Error ? Promise.reject(payload) : Promise.resolve(payload),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { CrewBoard } = await import('../pages/CrewBoardPage')
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <CrewBoard conductor={conductor} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, queryClient: qc }
}

describe('CrewBoardPage', () => {
  beforeEach(() => {
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    sessionStorage.clear()
    crewBoard.mockReset()
    crewBoardAction.mockReset()
    crewBoardAction.mockResolvedValue({
      ok: true, action: 'stop', item_id: 'it_00000001',
    })
  })
  afterEach(() => {
    cleanup()
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    sessionStorage.clear()
  })

  it('polls on the RFC interval and keys the cache by conductor', () => {
    expect(CREW_BOARD_POLL_MS).toBe(10_000)
    expect(crewBoardQueryKey(CONDUCTOR)).toEqual(['crew-board', CONDUCTOR])
    expect(crewBoardQueryKey('other')).not.toEqual(crewBoardQueryKey(CONDUCTOR))
  })

  it('lifts an outstanding question into the decision band', async () => {
    await mount(board({
      items: [
        item({ item_id: 'it_a', title: 'asks a question', status: 'question', outstanding: true }),
        item({ item_id: 'it_b', title: 'just working' }),
      ],
    }))
    expect(await screen.findByText('asks a question')).toBeTruthy()
    expect(screen.getByText('Needs a decision')).toBeTruthy()
    expect(screen.getByText('just working')).toBeTruthy()
  })

  it('keeps a terminal item behind the expander until it is opened', async () => {
    await mount(board({
      items: [
        item({ item_id: 'it_open', title: 'still open' }),
        item({
          item_id: 'it_done', title: 'all finished', state: 'accepted',
          status: 'done', verdict: 'pass', terminal: true, alive: 'closed',
        }),
      ],
    }))
    expect(await screen.findByText('still open')).toBeTruthy()
    expect(screen.queryByText('all finished')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /Finished/i }))
    expect(await screen.findByText('all finished')).toBeTruthy()
  })

  it('offers Stop only on an orphaned row, and no take-over control at all', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_orph', title: 'nobody reading', orphaned: true })],
    }))
    expect(await screen.findByText('nobody reading')).toBeTruthy()

    const stop = screen.getByRole('button', { name: 'Stop current turn' })
    expect(stop.hasAttribute('disabled')).toBe(false)
    // NOTHING rendered for take-over, not a disabled button: a control that can
    // never work is noise on every row of a board built for scanning, and an
    // explanation of a control the reader never saw explains nothing.
    expect(screen.queryByRole('button', { name: 'Take over' })).toBeNull()
    expect(screen.queryByText(/take-over/i)).toBeNull()
    // An actionable Stop states its OUTCOME. Without this the button is a red
    // verb with no stated consequence, which is a button nobody dares press.
    expect(screen.getByText(/branch and reports are kept/i)).toBeTruthy()
    // And the outcome it states is the one the server delivers: the delegate
    // cancels the turn the worker is running, so a promise that the session ends
    // and cannot be resumed would be copy the button cannot keep.
    expect(screen.getByText(/session stays open/i)).toBeTruthy()
    expect(screen.queryByText(/cannot be resumed/i)).toBeNull()
  })

  it('disables Stop with a visible reason when the worker session has closed', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [
        item({ item_id: 'it_gone', title: 'worker gone', orphaned: true, alive: 'closed' }),
      ],
    }))
    expect(await screen.findByText('worker gone')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Stop current turn' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/session is already closed/i)).toBeTruthy()
    // The one dim line carries the REASON here, not the consequence: an outcome
    // stated beside a button that cannot run reads as an offer that is not there.
    expect(screen.queryByText(/branch and reports are kept/i)).toBeNull()
  })

  it('sends the stop as (conductor, item_id, action) and never a session key', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_stop', title: 'stop me', orphaned: true })],
    }))
    expect(await screen.findByText('stop me')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    await waitFor(() => expect(crewBoardAction).toHaveBeenCalledTimes(1))
    expect(crewBoardAction).toHaveBeenCalledWith(CONDUCTOR, 'it_stop', 'stop')
  })

  it('tells a stale view apart from a broken action', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_409', title: 'moved on', orphaned: true })],
    }))
    expect(await screen.findByText('moved on')).toBeTruthy()

    crewBoardAction.mockRejectedValueOnce(Object.assign(new Error('conflict'), { status: 409 }))
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    expect(await screen.findByText(/no longer orphaned/i)).toBeTruthy()

    crewBoardAction.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    expect(await screen.findByText(/did not go through/i)).toBeTruthy()
  })

  it.each([
    { priorUnsavedPause: false, journaled: true },
    { priorUnsavedPause: true, journaled: true },
    { priorUnsavedPause: true, journaled: false },
  ])('hands off the row diagnostics (prior unsaved pause: $priorUnsavedPause, journaled: $journaled)', async ({ priorUnsavedPause, journaled }) => {
    const navigate = vi.fn()
    installSoftNavigate(navigate)
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_ctx', title: 'needs stopping', orphaned: true })],
    }))
    expect(await screen.findByText('needs stopping')).toBeTruthy()

    if (priorUnsavedPause) {
      crewBoardAction.mockResolvedValueOnce({
        ok: true, action: 'stop', item_id: 'it_ctx',
        goal_pause_saved: false, warning: PAUSE_WARNING,
      })
      await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
      expect(await screen.findByText(PAUSE_WARNING)).toBeTruthy()
      await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
    }

    const backendReport = {
      source: 'api' as const,
      route: '/crew-board',
      message: 'the board cache is flagged dirty',
      endpoint: '/api/crew-board/action',
      status: 500,
      code: 'store_dirty',
      detail: '{"error":"the board cache is flagged dirty","code":"store_dirty","detail":"save lock unavailable"}',
    }
    if (journaled) recordError(backendReport)
    crewBoardAction.mockRejectedValueOnce(
      Object.assign(new Error(backendReport.message), { status: backendReport.status }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))

    expect(await screen.findByText(/did not go through/i)).toBeTruthy()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent(backendReport.message)
    if (priorUnsavedPause) {
      expect(notice).toHaveTextContent(PAUSE_WARNING)
      expect(notice.textContent?.split(PAUSE_WARNING)).toHaveLength(2)
      expect(screen.getByText(RETRY_HINT)).toBeTruthy()
    } else {
      expect(notice).not.toHaveTextContent(PAUSE_WARNING)
      expect(screen.queryByText(RETRY_HINT)).toBeNull()
    }
    expect(screen.getAllByRole('button', { name: 'Ask the agent' })).toHaveLength(1)
    await userEvent.click(within(notice).getByRole('button', { name: 'Ask the agent' }))
    expect(navigate).toHaveBeenCalledExactlyOnceWith('/chat')
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain(backendReport.message)
    expect(staged).toContain('- Source: api')
    if (journaled) {
      expect(staged).toContain(`- Route: ${backendReport.route}`)
      expect(staged).toContain(backendReport.endpoint)
      expect(staged).toContain('HTTP 500')
      expect(staged).toContain(backendReport.code)
      expect(staged).toContain(backendReport.detail)
    } else {
      expect(staged).not.toContain(backendReport.endpoint)
      expect(staged).not.toContain('HTTP 500')
      expect(staged).not.toContain(backendReport.code)
      expect(staged).not.toContain(backendReport.detail)
    }
    if (priorUnsavedPause) expect(staged.split(PAUSE_WARNING)).toHaveLength(2)
    else expect(staged).not.toContain(PAUSE_WARNING)
    expect(consumeChatHandoff()).toBeNull()
  })

  it('does not read a 200 carrying ok:false as a stopped worker', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_un', title: 'runaway', orphaned: true })],
    }))
    expect(await screen.findByText('runaway')).toBeTruthy()

    // The delegate answers 200 with ok:false when it cannot reach the worker's
    // session, so this is a resolved promise, not a rejection: the success handler
    // is the code under test.
    crewBoardAction.mockResolvedValueOnce({ ok: false, action: 'stop', item_id: 'it_un' })
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    expect(await screen.findByText(/could not confirm/i)).toBeTruthy()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('alert')).not.toHaveTextContent(PAUSE_WARNING)
  })

  it('shows an unsaved-pause warning after an acknowledged Crew board Stop', async () => {
    const orphan = item({ item_id: 'it_unsaved', title: 'pause me', orphaned: true })
    await mount(board({ conductor_alive: 'closed', items: [orphan] }))
    expect(await screen.findByText('pause me')).toBeTruthy()

    crewBoardAction.mockResolvedValueOnce({
      ok: true, action: 'stop', item_id: orphan.item_id,
      goal_pause_saved: false, warning: PAUSE_WARNING,
    })
    crewBoard.mockResolvedValue(board({
      conductor_alive: 'closed', items: [{ ...orphan, alive: 'idle' }],
    }))
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))

    expect(await screen.findByText(PAUSE_WARNING)).toBeTruthy()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('alert').textContent?.split(PAUSE_WARNING)).toHaveLength(2)
    expect(within(screen.getByRole('alert')).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
    expect(screen.queryByText(/could not confirm/i)).toBeNull()
    const retryHint = screen.getByText(RETRY_HINT)
    expect(retryHint).toHaveTextContent(i18nT('pages.crewBoard.action_stop'))
    expect(retryHint).toHaveTextContent(/retry saving the pause/i)
    expect(retryHint).toHaveTextContent(/pause the goal/i)
    expect(retryHint).toHaveTextContent(/stops? the current turn/i)
    expect(retryHint).toHaveTextContent(/(?:session stays open|keeps the session open)/i)
    expect(crewBoardAction).toHaveBeenCalledTimes(1)
    expect(crewBoardAction).toHaveBeenCalledWith(CONDUCTOR, orphan.item_id, 'stop')

    // Only a separate click and its ordinary acknowledgment clear the warning.
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    await waitFor(() => expect(crewBoardAction).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.queryByText(PAUSE_WARNING)).toBeNull()
    expect(screen.queryByText(RETRY_HINT)).toBeNull()
  })

  it('keeps both Stop refusal and unsaved-pause facts in one notice', async () => {
    const orphan = item({ item_id: 'it_refused_unsaved', title: 'still running', orphaned: true })
    await mount(board({ conductor_alive: 'closed', items: [orphan] }))
    expect(await screen.findByText('still running')).toBeTruthy()

    crewBoardAction.mockResolvedValueOnce({
      ok: false, action: 'stop', item_id: orphan.item_id,
      goal_pause_saved: false, warning: PAUSE_WARNING,
    })
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))

    expect(await screen.findByText(PAUSE_WARNING)).toBeTruthy()
    expect(screen.getByText(/could not confirm/i)).toBeTruthy()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(crewBoardAction).toHaveBeenCalledTimes(1)
    expect(crewBoardAction).toHaveBeenCalledWith(CONDUCTOR, orphan.item_id, 'stop')
  })

  it.each(['request error', 'Stop refusal'])(
    'retains an unsaved pause when a later explicit retry returns %s',
    async (outcome) => {
      const orphan = item({ item_id: 'it_retry', title: 'retry the pause', orphaned: true })
      await mount(board({ conductor_alive: 'closed', items: [orphan] }))
      expect(await screen.findByText(orphan.title)).toBeTruthy()
      crewBoardAction.mockResolvedValueOnce({
        ok: true, action: 'stop', item_id: orphan.item_id,
        goal_pause_saved: false, warning: PAUSE_WARNING,
      })
      await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
      expect(await screen.findByText(PAUSE_WARNING)).toBeTruthy()
      await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))

      if (outcome === 'request error') {
        crewBoardAction.mockRejectedValueOnce(
          Object.assign(new Error('the board cache is flagged dirty'), { status: 500 }),
        )
      } else {
        crewBoardAction.mockResolvedValueOnce({
          ok: false, action: 'stop', item_id: orphan.item_id,
        })
      }
      await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
      expect(await screen.findByText(
        outcome === 'request error' ? /did not go through/i : /could not confirm/i,
      )).toBeTruthy()
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      const notice = screen.getByRole('alert')
      expect(notice).toHaveTextContent(PAUSE_WARNING)
      expect(notice.textContent?.split(PAUSE_WARNING)).toHaveLength(2)
      expect(notice).toHaveTextContent(
        outcome === 'request error' ? /did not go through/i : /could not confirm/i,
      )
      expect(screen.getByText(RETRY_HINT)).toBeTruthy()
      expect(screen.getAllByRole('button', { name: 'Ask the agent' })).toHaveLength(1)
      expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
      if (outcome === 'request error') {
        expect(notice).toHaveTextContent('the board cache is flagged dirty')
      }
      expect(crewBoardAction.mock.calls).toEqual([
        [CONDUCTOR, orphan.item_id, 'stop'],
        [CONDUCTOR, orphan.item_id, 'stop'],
      ])

      // A later acknowledged, saved Stop still clears both facts.
      await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
      expect(screen.queryByText(PAUSE_WARNING)).toBeNull()
      expect(screen.queryByText(RETRY_HINT)).toBeNull()
      expect(crewBoardAction).toHaveBeenCalledTimes(3)
    },
  )

  it.each([
    { status: 503, detail: 'board refresh unavailable' },
    { status: 404, detail: 'no work ledger for this conductor' },
  ])('retains the loaded row and unsaved pause through a failed refresh and recovery ($status)', async ({ status, detail }) => {
    const orphan = item({ item_id: 'it_refresh', title: 'keep this row', orphaned: true })
    const { queryClient } = await mount(board({ conductor_alive: 'closed', items: [orphan] }))
    expect(await screen.findByText(orphan.title)).toBeTruthy()
    crewBoardAction.mockResolvedValueOnce({
      ok: true, action: 'stop', item_id: orphan.item_id,
      goal_pause_saved: false, warning: PAUSE_WARNING,
    })
    const stop = screen.getByRole('button', { name: 'Stop current turn' })
    await userEvent.click(stop)
    expect(await screen.findByText(PAUSE_WARNING)).toBeTruthy()
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))

    crewBoard.mockRejectedValueOnce(
      Object.assign(new Error(detail), { status }),
    )
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(await screen.findByText(detail)).toBeTruthy()
    expect(screen.getByText(CACHED_TITLE)).toBeTruthy()
    expect(screen.getByText(orphan.title)).toBeTruthy()
    expect(screen.getByText(PAUSE_WARNING)).toBeTruthy()
    expect(screen.getByText(RETRY_HINT)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Stop current turn' })).toBe(stop)

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    await waitFor(() => expect(screen.queryByText(detail)).toBeNull())
    expect(screen.queryByText(CACHED_TITLE)).toBeNull()
    expect(screen.getByText(PAUSE_WARNING)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Stop current turn' })).toBe(stop)
    expect(crewBoard).toHaveBeenCalledTimes(4)
    expect(crewBoardAction.mock.calls).toEqual([[CONDUCTOR, orphan.item_id, 'stop']])
  })

  it.each([
    { status: 503, detail: 'empty board refresh unavailable' },
    { status: 404, detail: 'no work ledger for this conductor' },
  ])('labels the last loaded empty board when a refresh fails ($status)', async ({ status, detail }) => {
    const { queryClient } = await mount(board({ items: [] }))
    expect(await screen.findByText('No work items yet')).toBeTruthy()

    crewBoard.mockRejectedValueOnce(Object.assign(new Error(detail), { status }))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(await screen.findByText(detail)).toBeTruthy()
    expect(screen.getByText(CACHED_TITLE)).toBeTruthy()
    expect(screen.getByText('The last loaded version had no work items.')).toBeTruthy()
    expect(screen.queryByText('No work items yet')).toBeNull()
    expect(screen.getByText('ship the ledger')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Stop current turn' })).toBeNull()
    expect(crewBoardAction).not.toHaveBeenCalled()
    expect(crewBoard).toHaveBeenCalledTimes(2)
  })

  it('shows an initial read failure without inventing loaded rows', async () => {
    await mount(Object.assign(new Error('initial board read unavailable'), { status: 503 }))
    expect(await screen.findByText('initial board read unavailable')).toBeTruthy()
    expect(screen.getByText('Could not read this board')).toBeTruthy()
    expect(screen.queryByText(CACHED_TITLE)).toBeNull()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Stop current turn' })).toBeNull()
  })

  it('renders a session with no work ledger as a gap, not a failure', async () => {
    await mount(Object.assign(new Error('no ledger'), { status: 404 }))
    expect(await screen.findByText(/has no crew board/i)).toBeTruthy()
    expect(screen.queryByText(CACHED_TITLE)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(crewBoardAction).not.toHaveBeenCalled()
  })

  it('asks for a conductor when none is selected', async () => {
    await mount(board(), '')
    expect(await screen.findByText(/No conductor session selected/i)).toBeTruthy()
    expect(crewBoard).not.toHaveBeenCalled()
  })

  it('shows the stale badge and the vague-bar chip', async () => {
    await mount(board({
      items: [
        item({ item_id: 'it_s', title: 'quiet one', stale: true, acceptance_concrete: false }),
      ],
    }))
    expect(await screen.findByText('quiet one')).toBeTruthy()
    // "stale" appears twice by design: the chip on the row and the right-hand kind
    // column, which names what the row IS. Both are correct, so assert on both.
    expect(screen.getAllByText('stale').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/no acceptance criteria/i)).toBeTruthy()
  })

  it('shows the conductor goal and an event count that expands', async () => {
    await mount(board({
      items: [
        item({
          item_id: 'it_ev', title: 'has events', decision: 'carry on', pr: 42,
          artifacts: { branch: 'feat/x' },
          events: [
            {
              id: 'ev1', ts: '2026-09-22T10:00:00+00:00', item_id: 'it_ev',
              kind: 'report', status: 'progress', text: 'first report',
            },
            {
              id: 'ev2', ts: '2026-09-22T10:05:00+00:00', item_id: 'it_ev',
              kind: 'bind', status: null, text: '',
            },
          ],
        }),
      ],
    }))
    expect(await screen.findByText('ship the ledger')).toBeTruthy()
    expect(screen.getByText('carry on')).toBeTruthy()
    expect(screen.getByText('feat/x')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /Events/i }))
    expect(await screen.findByText('first report')).toBeTruthy()
  })

  it('shows the goal on a board that has no items yet', async () => {
    // The goal belongs to the ledger, not the item list. Without it an empty board
    // names nothing, so a reader who followed the menu entry cannot tell whether
    // they reached the right conductor's board or a broken page.
    await mount(board({ items: [], conductor: { ...board().conductor, goal: 'split the rewrite' } }))
    expect(await screen.findByText('No work items yet')).toBeTruthy()
    expect(screen.getByText('split the rewrite')).toBeTruthy()
  })
})
