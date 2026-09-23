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
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
