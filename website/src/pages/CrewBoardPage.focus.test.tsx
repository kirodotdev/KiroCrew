// Focus on an orphaned row's Stop button while its request is out.
//
// A native `disabled` on a focused button blurs it to `<body>`, so a keyboard user
// who pressed Stop lost their place on the board before the failure notice even
// rendered. The button stays focusable and announces itself as unavailable instead.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
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

describe('CrewBoardPage PR facts', () => {
  afterEach(() => {
    cleanup()
    crewBoard.mockReset()
  })

  it('names the PR the facts were read for, apart from the PR the worker reported', async () => {
    crewBoard.mockResolvedValue({
      ...BOARD,
      items: [{ ...ORPHAN, pr: 9 }],
      pr_facts: {
        [ORPHAN.item_id]: {
          label: 'acme/w#7',
          url: 'https://github.com/acme/w/pull/7',
          reads: { warm_brief: { facts: { state: 'merged' }, observed_at: 10 } },
        },
      },
    } as WorkBoardResponse)
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
    // "merged" sits beside the PR it was read for (#7), not the reported #9.
    expect(await screen.findByTestId('crew-board-pr-facts-label')).toHaveTextContent('acme/w#7')
    const facts = screen.getAllByTestId('crew-board-pr-fact')
    expect(facts.map((t) => t.textContent)).toContain('merged')
    // PR state is a shared status badge; the read time stays plain metadata.
    const merged = facts.find((t) => t.textContent === 'merged')!
    expect(merged.querySelector('.rounded-full')).not.toBeNull()
    const read = facts.find((t) => t.textContent?.startsWith('read '))!
    expect(read.querySelector('.rounded-full')).toBeNull()
  })
})
