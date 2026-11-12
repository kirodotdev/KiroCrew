// Focus on an orphaned row's Stop button while its request is out.
//
// A native `disabled` on a focused button blurs it to `<body>`, so a keyboard user
// who pressed Stop lost their place on the board before the failure notice even
// rendered. The button stays focusable and announces itself as unavailable instead.
// When a re-read finds the item no longer orphaned the button unmounts, and focus
// moves to the row's title rather than falling to `<body>`.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../store'
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

const ORPHAN: WorkBoardItem = {
  schema: 1,
  item_id: 'it_focus',
  title: 'keep my place',
  acceptance: { kind: 'pr_checks', pr: 1, repo: 'o/r' },
  state: 'open',
  verdict: null,
  decision: '',
  round: 0,
  fails: 0,
  status: 'progress',
  summary: '',
  artifacts: {},
  pr: null,
  last_report_at: '2026-09-22T10:00:00+00:00',
  created_at: '2026-09-22T09:00:00+00:00',
  closed_at: null,
  decided_at: null,
  orphaned: true,
  stale: false,
  acceptance_concrete: true,
  outstanding: false,
  terminal: false,
  alive: 'running',
  events: [],
}

const BOARD: WorkBoardResponse = {
  conductor: {
    schema: 1,
    slot_key: CONDUCTOR,
    goal: 'g',
    round: 1,
    depth: 0,
    parent_item: null,
    created_at: '2026-09-22T08:00:00+00:00',
  },
  conductor_alive: 'closed',
  items: [ORPHAN],
  take_over_available: false,
}

async function mount() {
  crewBoard.mockResolvedValue(BOARD)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { CrewBoard } = await import('./CrewBoardPage')
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <CrewBoard conductor={CONDUCTOR} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  expect(await screen.findByText('keep my place')).toBeTruthy()
  return screen.getByRole('button', { name: 'Stop current turn' })
}

describe('CrewBoardPage Stop focus', () => {
  afterEach(() => {
    cleanup()
    crewBoard.mockReset()
    crewBoardAction.mockReset()
  })

  it('moves focus to the row title when the re-read after Stop clears orphaned', async () => {
    crewBoardAction.mockResolvedValue({ ok: true, action: 'stop', item_id: ORPHAN.item_id })
    const stop = await mount()
    // The conductor came back: the row stays, and its orphan-only actions go.
    crewBoard.mockResolvedValue({ ...BOARD, conductor_alive: 'idle', items: [{ ...ORPHAN, orphaned: false }] })

    stop.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop current turn' })).toBeNull())
    expect(document.activeElement).toBe(screen.getByText('keep my place'))
  })

  it('does not throw when the re-read moves the whole row into the collapsed Finished band', async () => {
    crewBoardAction.mockResolvedValue({ ok: true, action: 'stop', item_id: ORPHAN.item_id })
    const stop = await mount()
    // The title unmounts with its row, so there is nothing to move focus to.
    crewBoard.mockResolvedValue({
      ...BOARD,
      items: [{ ...ORPHAN, orphaned: false, terminal: true, state: 'abandoned' }],
    })

    stop.focus()
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByRole('button', { name: /Finished/ })).toBeTruthy()
    expect(screen.queryByText('keep my place')).toBeNull()
  })

  it('keeps focus on Stop while the request is out, and refuses a second press', async () => {
    let reject: (err: unknown) => void = () => {}
    crewBoardAction.mockImplementation(() => new Promise((_resolve, rej) => { reject = rej }))
    const stop = await mount()

    stop.focus()
    await userEvent.keyboard('{Enter}')
    expect(crewBoardAction).toHaveBeenCalledTimes(1)
    expect(stop.getAttribute('aria-disabled')).toBe('true')
    expect(stop.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(stop)

    // Inert while pending: the guard is in the handler, not the attribute.
    await userEvent.keyboard('{Enter}')
    expect(crewBoardAction).toHaveBeenCalledTimes(1)

    reject(Object.assign(new Error('boom'), { status: 500 }))
    expect(await screen.findByText(/did not go through/i)).toBeTruthy()
    expect(document.activeElement).toBe(stop)
    expect(stop.hasAttribute('aria-disabled')).toBe(false)
  })
})
