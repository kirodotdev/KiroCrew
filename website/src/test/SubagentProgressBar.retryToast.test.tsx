/**
 * The "Retry failed" toast's granularity, rendered.
 *
 * The progress-bar retry fan-out used to collapse every outcome into one
 * countless, reasonless line: `results.some(rejected) -> "Couldn't retry some
 * of the failed subagents"`. That made a benign, self-caused race — a
 * double-click that trips a single `409 retry_superseded`, where the run was
 * already picked up and retrying again would double-run its task — read exactly
 * like a real outage. The sibling `activityViewer` fan-out already did better by
 * carrying `{count}`; this surface should at least match it and, for the
 * all-benign case, say nothing at all (the failed set is already drained).
 *
 * These tests drive the real retry handler with a MIXED settled set and read
 * what the user would read on the chip, mirroring the harness in
 * SubagentProgressBar.retryStop.i18n.test.tsx.
 */

import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentDone } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ApiError } from '../api/apiError'

const spawnRetry = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    spawnRetry: (id: string) => spawnRetry(id),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import { i18next } from '../i18n/all'

const SLOT = 'test-slot'
const RETRY_BTN = 'pages.chat.subagentProgressBar.retry_failed_count'

/** `n` terminally-failed agents, so the "Retry failed (n)" button is shown. */
function renderChip(failedIds: string[]) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  for (const id of failedIds) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id, elapsed: 1, error: 'boom', outcome: 'failed' }))
  }
  // One still-running agent so the chip renders (`hasActive`): the realistic
  // state for this bug is a double-click while a wave is still in flight.
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'running-1', task: 'task running', agent: 'agent-running' }))
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
  const clickRetry = () => {
    const label = i18next.t(RETRY_BTN, { count: failedIds.length }) as string
    const btn = [...utils.container.querySelectorAll('button')].find(b => (b.textContent || '').includes(label))
    expect(btn, 'no retry button rendered').toBeTruthy()
    fireEvent.click(btn!)
  }
  return { ...utils, clickRetry }
}

/** A 409 the backend sends with `code: retry_superseded` — the benign race. */
const superseded = () =>
  Promise.reject(new ApiError(409, 'already picked up', JSON.stringify({ error: 'run x', code: 'retry_superseded' })))

/** A real failure — a 404 with no benign code. */
const notFound = () =>
  Promise.reject(new ApiError(404, 'not found', JSON.stringify({ error: 'not found' })))

beforeEach(() => {
  spawnRetry.mockReset()
})

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('SubagentProgressBar — retry-failed toast granularity', () => {
  it('does NOT show a failure toast when the only rejection is a benign retry_superseded race', async () => {
    await i18next.changeLanguage('en')
    // Two failed agents; the user double-clicked, so one id is already drained
    // (retry_superseded) and the other retries fine.
    spawnRetry.mockImplementation((id: string) => (id === 'f1' ? superseded() : Promise.resolve({})))
    const { queryByTestId, clickRetry } = renderChip(['f1', 'f2'])
    clickRetry()
    // Give the allSettled().then() a tick to run.
    await waitFor(() => expect(spawnRetry).toHaveBeenCalledTimes(2))
    await new Promise(r => setTimeout(r, 0))
    expect(
      queryByTestId('subagent-action-error'),
      'an all-benign retry_superseded race should read as a no-op, not a failure toast',
    ).toBeNull()
  })

  it('shows a count in the toast for a genuine failure, like the activityViewer sibling', async () => {
    await i18next.changeLanguage('en')
    // One real 404, one benign superseded: 1 genuine failure to report.
    spawnRetry.mockImplementation((id: string) => (id === 'f1' ? notFound() : superseded()))
    const { findByTestId, clickRetry } = renderChip(['f1', 'f2'])
    clickRetry()
    const notice = await findByTestId('subagent-action-error')
    // The toast must carry the genuine-failure count (1), not a countless "some".
    expect(notice.textContent || '').toMatch(/\b1\b/)
    expect(notice.textContent || '', 'the countless "some" copy is back').not.toMatch(/\bsome\b/i)
  })
})
