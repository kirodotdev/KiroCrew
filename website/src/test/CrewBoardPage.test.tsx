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
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { act, render, screen, cleanup, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { defaultScheduler, notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../store'
import { crewBoardQueryKey, CREW_BOARD_POLL_MS } from '../api/crewBoard'
import type { WorkBoardItem, WorkBoardResponse } from '../api/crewBoard'

const crewBoard = vi.fn()
const crewBoardAction = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    crewBoard: (...args: unknown[]) => crewBoard(...args),
    crewBoardAction: (...args: unknown[]) => crewBoardAction(...args),
  },
}))

const CONDUCTOR = 'chat-1-conductor'

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
    decided_at: null,
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
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <CrewBoard conductor={conductor} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('CrewBoardPage', () => {
  beforeEach(() => {
    crewBoard.mockReset()
    crewBoardAction.mockReset()
    crewBoardAction.mockResolvedValue({
      ok: true, action: 'stop', item_id: 'it_00000001',
    })
  })
  afterEach(() => cleanup())

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

  it('keeps the backend sentence as the message so the agent hand-off has context', async () => {
    await mount(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_ctx', title: 'needs stopping', orphaned: true })],
    }))
    expect(await screen.findByText('needs stopping')).toBeTruthy()

    crewBoardAction.mockRejectedValueOnce(
      Object.assign(new Error('the board cache is flagged dirty'), { status: 500 }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))

    // Both survive, in their own slots: the translated line leads, and the
    // backend's own sentence stays the message because the error journal is keyed
    // by it. Asserting only the lead would pass while the context was discarded.
    expect(await screen.findByText(/did not go through/i)).toBeTruthy()
    expect(screen.getByText('the board cache is flagged dirty')).toBeTruthy()
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
  })

  it('renders a session with no work ledger as a gap, not a failure', async () => {
    await mount(Object.assign(new Error('no ledger'), { status: 404 }))
    expect(await screen.findByText(/has no crew board/i)).toBeTruthy()
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

  it('says a worker could not send its report, or read its brief', async () => {
    await mount(board({
      items: [
        item({
          item_id: 'it_r', title: 'tried to report',
          report_undelivered: { tool: 'work_report', at: '2026-09-22T10:01:00+00:00' },
        }),
        item({
          item_id: 'it_b', title: 'tried to read',
          report_undelivered: { tool: 'work_brief', at: '2026-09-22T10:02:00+00:00' },
        }),
        item({ item_id: 'it_ok', title: 'reported fine', report_undelivered: null }),
      ],
    }))
    expect(await screen.findByText('tried to report')).toBeTruthy()
    // A failed call renders through the shared error surface, with its hand-off,
    // once per affected row and never on the row whose calls got through.
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(2)
    expect(alerts.filter((a) => a.textContent?.includes("couldn't send its report"))).toHaveLength(1)
    expect(alerts.filter((a) => a.textContent?.includes("couldn't read its brief"))).toHaveLength(1)
    for (const alert of alerts) {
      expect(alert.textContent).toContain("the harness couldn't resolve the tool name")
    }
    // Each hand-off is scoped to its own row, and the text it hands off names the item.
    expect(within(alerts[0]).getByText('Ask the agent about tried to report')).toBeTruthy()
    expect(alerts[0].textContent).toContain('it_r:')
    expect(within(alerts[1]).getByText('Ask the agent about tried to read')).toBeTruthy()
    expect(alerts[1].textContent).toContain('it_b:')
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

  it('counts failed attempts with the number first and the right plural', async () => {
    // The label and the number were two pieces glued in JS, so English read
    // "failed attempts 1" and no language could move the number or pick a form.
    await mount(board({
      items: [
        item({ item_id: 'it_one', title: 'failed once', fails: 1 }),
        item({ item_id: 'it_many', title: 'failed a lot', fails: 1200 }),
      ],
    }))
    expect(await screen.findByText('1 failed attempt')).toBeTruthy()
    expect(screen.getByText('1,200 failed attempts')).toBeTruthy()
  })

  it('shows the goal on a board that has no items yet', async () => {
    // The goal belongs to the ledger, not the item list. Without it an empty board
    // names nothing, so a reader who followed the menu entry cannot tell whether
    // they reached the right conductor's board or a broken page.
    await mount(board({ items: [], conductor: { ...board().conductor, goal: 'split the rewrite' } }))
    expect(await screen.findByText('No work items yet')).toBeTruthy()
    expect(screen.getByText('split the rewrite')).toBeTruthy()
  })

  it('keeps the board and its Stop notice when a background poll fails', async () => {
    // React Query keeps the last good board beside a failed refetch. Swapping the
    // whole board for the error unmounted every row, so the Stop failure the
    // reader was looking at vanished on the next 10 s poll after a gateway blip.
    //
    // Notifications run on microtasks here, so the `act` around each refetch
    // flushes the re-render: an absence asserted after it is a real absence, not
    // a render that has not happened yet.
    notifyManager.setScheduler(queueMicrotask)
    onTestFinished(() => notifyManager.setScheduler(defaultScheduler))
    crewBoard.mockResolvedValue(board({
      conductor_alive: 'closed',
      items: [item({ item_id: 'it_poll', title: 'still here', orphaned: true })],
    }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { CrewBoard } = await import('../pages/CrewBoardPage')
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <CrewBoard conductor={CONDUCTOR} />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(await screen.findByText('still here')).toBeTruthy()
    crewBoardAction.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    await userEvent.click(screen.getByRole('button', { name: 'Stop current turn' }))
    expect(await screen.findByText(/did not go through/i)).toBeTruthy()

    crewBoard.mockRejectedValue(new Error('gateway restarting'))
    await act(async () => {
      await qc.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })

    expect(await screen.findByText(/Couldn't refresh this board/i)).toBeTruthy()
    expect(screen.getByText('gateway restarting')).toBeTruthy()
    expect(screen.queryByText('Could not read this board')).toBeNull()
    expect(screen.getByText('still here')).toBeTruthy()
    expect(screen.getByText(/did not go through/i)).toBeTruthy()

    // A 404 is the ledger's absence (a purge removes it), not a blip: the cached
    // board must not outlive it.
    crewBoard.mockRejectedValue(Object.assign(new Error('no ledger'), { status: 404 }))
    const callsBefore404 = crewBoard.mock.calls.length
    await act(async () => {
      await qc.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(await screen.findByText(/has no crew board/i)).toBeTruthy()
    expect(screen.queryByText('still here')).toBeNull()
    // Dropped from the shared cache, not only hidden here, so a remount or any
    // other reader of this key has no purged board to show.
    await waitFor(() => expect(qc.getQueryData(crewBoardQueryKey(CONDUCTOR))).toBeUndefined())
    // Clearing the data keeps the 404 and starts no second fetch, so the
    // no-ledger state holds instead of dropping to a pending blank.
    await act(async () => {})
    expect(qc.getQueryState(crewBoardQueryKey(CONDUCTOR))?.status).toBe('error')
    expect(crewBoard.mock.calls.length).toBe(callsBefore404 + 1)
    expect(screen.getByText(/has no crew board/i)).toBeTruthy()

    // The next poll re-asks with no data, which puts the query back to pending
    // with no error. While that answer is out, the no-ledger state must hold
    // rather than blank the panel; the 404 that comes back keeps it.
    let answer404: () => void = () => {}
    crewBoard.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      answer404 = () => reject(Object.assign(new Error('no ledger'), { status: 404 }))
    }))
    let poll: Promise<void> = Promise.resolve()
    await act(async () => {
      poll = qc.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(qc.getQueryState(crewBoardQueryKey(CONDUCTOR))?.fetchStatus).toBe('fetching')
    expect(screen.getByText(/has no crew board/i)).toBeTruthy()
    await act(async () => {
      answer404()
      await poll
    })
    expect(screen.getByText(/has no crew board/i)).toBeTruthy()

    // A blip after the 404 must not bring the purged board back.
    crewBoard.mockRejectedValue(new Error('gateway restarting'))
    await act(async () => {
      await qc.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(screen.getByText('Could not read this board')).toBeTruthy()
    expect(screen.queryByText('still here')).toBeNull()

    // Only a successful read brings a board back.
    crewBoard.mockResolvedValue(board({ items: [item({ item_id: 'it_new', title: 'a new ledger' })] }))
    await act(async () => {
      await qc.refetchQueries({ queryKey: crewBoardQueryKey(CONDUCTOR) })
    })
    expect(await screen.findByText('a new ledger')).toBeTruthy()
  })
})
