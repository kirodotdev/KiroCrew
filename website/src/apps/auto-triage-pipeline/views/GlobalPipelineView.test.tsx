// GlobalPipelineView — the shell that owns the drill-down between L0, L1 and L2.
//
// The three level components have their own tests. What is only testable HERE is
// the shell's behaviour: that the levels STACK instead of replacing, that each
// level fetches only while it is open, that a failed fetch renders as a failure
// rather than as a confident empty result, and the two guards that exist because
// an earlier revision asserted something false -- the omitted item count and the
// number-guarded L2 render.
//
// The api module is mocked at its seam so the queries resolve synchronously; the
// child views are real, because asserting on what an operator actually sees is
// the point of a shell test.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../../../store'

const { overview, step, itemSessions } = vi.hoisted(() => ({
  overview: vi.fn(),
  step: vi.fn(),
  itemSessions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    autoTriagePipelineFoldApi: { overview, step, itemSessions },
  }
})

import GlobalPipelineView from './GlobalPipelineView'
import type { ItemSession, OverviewResponse, OverviewStep, StepItem } from '../api'

// ── fixtures ──────────────────────────────────────────────────────────────────

function overviewStep(over: Partial<OverviewStep> = {}): OverviewStep {
  return {
    key: 'implement',
    label: 'Implement',
    unit: 'issues',
    entered: 10,
    done: 4,
    skipped: 1,
    churn: 2,
    recentEntered: 3,
    recentDone: 1,
    inFlight: 5,
    distinctEntered: 8,
    distinctDone: 4,
    ...over,
  }
}

function overviewResponse(steps: OverviewStep[]): OverviewResponse {
  return {
    steps,
    totalEvents: 3826,
    unparseable: 0,
    unmappedEvents: [],
    firstEventAt: 1_700_000_000,
    lastEventAt: 1_700_090_000,
    recentHours: 24,
  }
}

function stepItem(over: Partial<StepItem> = {}): StepItem {
  return {
    number: 5546,
    title: 'An issue that is sitting in the step',
    labels: [],
    author: '',
    assignees: [],
    comments: null,
    queuedAt: null,
    dispatchedAt: null,
    resumeCount: 0,
    slot: 'chat:1',
    previousSlots: [],
    withdrawn: false,
    needsHuman: false,
    pr: null,
    lastEvent: 'implement_start',
    lastEventAt: null,
    ...over,
  }
}

function session(over: Partial<ItemSession> = {}): ItemSession {
  return {
    slot: 'chat:1',
    model: 'sonnet',
    agent: 'kirocrew',
    surface: 'dashboard',
    current: true,
    startedAt: null,
    lastAt: null,
    turns: 3,
    input: 0,
    output: 0,
    cacheCreate: 0,
    cacheRead: 0,
    cost: 0,
    credits: 17.75,
    durationMs: 0,
    contextUsed: 0,
    contextWindow: 0,
    lastPhase: '',
    lastStopReason: '',
    ...over,
  }
}

function renderView() {
  // Retries off, so a rejection surfaces as isError on the first attempt instead
  // of after react-query's default backoff.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  // The L2 table reads the LIVE store to decide whether a failed session switch
  // still owns the chat surface, and navigates to /chat on a successful one, so
  // rendering it for real needs both a Provider and a Router.
  return render(
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <GlobalPipelineView />
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>,
  )
}

beforeEach(() => {
  overview.mockResolvedValue(overviewResponse([overviewStep()]))
  step.mockResolvedValue({ step: 'implement', count: 1, items: [stepItem()] })
  itemSessions.mockResolvedValue({
    number: 5546,
    count: 1,
    sessions: [session()],
    populatedColumns: ['credits', 'turns'],
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

// ── L0 ────────────────────────────────────────────────────────────────────────

describe('GlobalPipelineView — L0', () => {
  it('renders the pipeline once the overview resolves', async () => {
    renderView()
    expect(await screen.findByText('Implement')).toBeTruthy()
  })

  it('renders a FAILURE with a retry when the overview fails, never an empty state', async () => {
    // The empty state is a factual claim ("no pipeline activity yet") that an
    // operator cannot distinguish from the truth, so a failed read must not
    // borrow it.
    overview.mockRejectedValue(new Error('boom'))
    renderView()
    expect(await screen.findByTestId('atp-overview-error')).toBeTruthy()
    expect(screen.queryByTestId('atp-no-pipeline')).toBeNull()

    overview.mockResolvedValue(overviewResponse([overviewStep()]))
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText('Implement')).toBeTruthy()
  })

  it('shows the designed empty state when the pipeline really has no steps', async () => {
    overview.mockResolvedValue(overviewResponse([]))
    renderView()
    expect(await screen.findByTestId('atp-no-pipeline')).toBeTruthy()
    expect(screen.queryByTestId('atp-overview-error')).toBeNull()
  })

  it('does NOT fetch L1 or L2 until a level is opened', async () => {
    renderView()
    await screen.findByText('Implement')
    expect(step).not.toHaveBeenCalled()
    expect(itemSessions).not.toHaveBeenCalled()
  })
})

// ── L0 -> L1 ──────────────────────────────────────────────────────────────────

describe('GlobalPipelineView — drilling into a step', () => {
  it('KEEPS the pipeline in view when a step is opened, rather than replacing it', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    // The item row and the step heading are L1; the card that was clicked is L0
    // and must still be there, because drilling in is a comparison.
    expect(await screen.findByText(/An issue that is sitting in the step/)).toBeTruthy()
    expect(await screen.findByText(/Implement · 1 item/)).toBeTruthy()
    expect(screen.getByText('Implement')).toBeTruthy()
  })

  it('closes the step when the same step is clicked again', async () => {
    renderView()
    const card = await screen.findByText('Implement')
    fireEvent.click(card)
    await screen.findByText(/An issue that is sitting in the step/)
    fireEvent.click(screen.getByText('Implement'))
    await waitFor(() =>
      expect(screen.queryByText(/An issue that is sitting in the step/)).toBeNull(),
    )
  })

  it('OMITS the count until it is known instead of asserting zero', async () => {
    // `count ?? 0` rendered "Implement - 0 items" for one refetch cycle on every
    // drill-in, directly above the rows that contradicted it.
    let resolveStep: (v: unknown) => void = () => {}
    step.mockImplementationOnce(() => new Promise((res) => { resolveStep = res }))
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    await waitFor(() => expect(screen.queryByText(/0 items/)).toBeNull())

    resolveStep({ step: 'implement', count: 1, items: [stepItem()] })
    expect(await screen.findByText(/1 item/)).toBeTruthy()
  })

  it('heads the section with the LOCALIZED step label, not the raw key', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    // "implement" would appear directly under "Implement" as if they differed.
    expect(await screen.findByText(/Implement · 1 item/)).toBeTruthy()
  })

  it('renders a failure with a retry when the step listing fails', async () => {
    step.mockRejectedValue(new Error('boom'))
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    expect(await screen.findByTestId('atp-step-error')).toBeTruthy()

    step.mockResolvedValue({ step: 'implement', count: 1, items: [stepItem()] })
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText(/An issue that is sitting in the step/)).toBeTruthy()
  })

  it('closes the step from the close control', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    await screen.findByText(/An issue that is sitting in the step/)
    fireEvent.click(screen.getByLabelText('Close step'))
    await waitFor(() =>
      expect(screen.queryByText(/An issue that is sitting in the step/)).toBeNull(),
    )
  })
})

// ── L1 -> L2 ──────────────────────────────────────────────────────────────────

describe('GlobalPipelineView — drilling into an item', () => {
  it('renders the item sessions inside the row that owns them', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    fireEvent.click(await screen.findByText(/An issue that is sitting in the step/))
    await waitFor(() => expect(itemSessions).toHaveBeenCalledWith(5546))
    expect(await screen.findByTestId('atp-session-chat:1')).toBeTruthy()
  })

  it('renders a failure with a retry when the session listing fails', async () => {
    itemSessions.mockRejectedValue(new Error('boom'))
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    fireEvent.click(await screen.findByText(/An issue that is sitting in the step/))
    expect(await screen.findByTestId('atp-sessions-error')).toBeTruthy()
  })

  it('collapses the item when it is clicked again, and stops showing its sessions', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    const row = await screen.findByText(/An issue that is sitting in the step/)
    fireEvent.click(row)
    await screen.findByTestId('atp-session-chat:1')
    fireEvent.click(screen.getByText(/An issue that is sitting in the step/))
    await waitFor(() => expect(screen.queryByTestId('atp-session-chat:1')).toBeNull())
  })

  it('clears the open item when a DIFFERENT step is chosen', async () => {
    // Otherwise the new step's list would arrive with a row already expanded on a
    // number that belongs to the step just left.
    overview.mockResolvedValue(
      overviewResponse([overviewStep(), overviewStep({ key: 'verify', label: 'Verify' })]),
    )
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    fireEvent.click(await screen.findByText(/An issue that is sitting in the step/))
    await screen.findByTestId('atp-session-chat:1')

    fireEvent.click(screen.getByText('Verify'))
    await waitFor(() => expect(screen.queryByTestId('atp-session-chat:1')).toBeNull())
  })
})

// ── refresh ───────────────────────────────────────────────────────────────────

describe('GlobalPipelineView — refresh', () => {
  it('refetches only the levels that are open', async () => {
    renderView()
    await screen.findByText('Implement')
    overview.mockClear()

    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(overview).toHaveBeenCalledTimes(1))
    // L1 and L2 are closed, so refreshing must not pay for their reads.
    expect(step).not.toHaveBeenCalled()
    expect(itemSessions).not.toHaveBeenCalled()
  })

  it('refetches the open step as well once one is chosen', async () => {
    renderView()
    fireEvent.click(await screen.findByText('Implement'))
    await screen.findByText(/An issue that is sitting in the step/)
    step.mockClear()

    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(step).toHaveBeenCalledTimes(1))
  })
})
