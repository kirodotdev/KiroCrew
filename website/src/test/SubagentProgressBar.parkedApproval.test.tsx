/**
 * A sub-agent parked on an unanswered spawn approval must not render as running
 * (#7318).
 *
 * The wave chip's header count and its per-agent row both treated `'pending'` as
 * running: the count sat behind a spinning `Loader2` and the row rendered a bare
 * task label with a ticking elapsed timer -- pixel-identical to an agent that had
 * launched a process and was working. The run had in fact launched nothing: it
 * was registered, counted, and blocked on an approval prompt the user had not
 * answered, so the one number the chip exists to publish was asserting the
 * opposite of the truth.
 *
 * These pin the acceptance criteria: the parked run is excluded from the running
 * count, it is reported under its own count instead, its row names the approval,
 * and the chip stays mounted when the parked run is the ONLY member of the wave
 * (excluding it from `running` must not make the surface disappear).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot, sseSubagentPending, sseSubagentSpawn, sseSubagentStalled,
  markSubagentApprovalGone, selectSidebarApprovalCounts, sseSubagentTool, reconcileGoneSubagent,
} from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: { spawnDelete: vi.fn().mockResolvedValue({}), spawnList: vi.fn().mockResolvedValue({ agents: [] }) },
}))

import { api } from '../api/client'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'

const SLOT = 'test-slot'
const PARKED_LABEL = 'Waiting for your approval to start'
const GONE_ERROR = 'This approval has expired or was already decided'
const CHECKING = 'Checking whether it started…'

function chip({ parked = 0, running = 0, seed }: {
  parked?: number
  running?: number
  /** Extra frames folded in BEFORE render — a dispatch after render would need
   *  act() and, if forgotten, silently asserts against the first paint. */
  seed?: (dispatch: (a: unknown) => void) => void
} = {}) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  for (let i = 0; i < parked; i++) {
    // The real producer: useWebSocket routes an `approval` frame whose id is
    // `spawn:<agent_id>` into sseSubagentPending, which is the only writer of
    // status 'pending' and always carries the approval_id.
    store.dispatch(sseSubagentPending({ slot: SLOT, id: `p${i}`, task: `parked ${i}`, approval_id: `spawn:p${i}` }))
  }
  for (let i = 0; i < running; i++) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: `r${i}`, task: `running ${i}`, agent: 'kirocrew' }))
  }
  seed?.(store.dispatch as unknown as (a: unknown) => void)
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><SubagentProgressBar slot={SLOT} /></Provider>
    </QueryClientProvider>,
  )
  return { container, store }
}

const text = (el: Element | null) => (el?.textContent ?? '').trim()
const runningCount = (c: HTMLElement) => text(c.querySelector('[data-testid="subagent-running-count"]'))
const awaitingCount = (c: HTMLElement) => text(c.querySelector('[data-testid="subagent-awaiting-count"]'))
const unresolvedCount = (c: HTMLElement) => text(c.querySelector('[data-testid="subagent-unresolved-count"]'))

beforeEach(() => vi.clearAllMocks())

describe('subagent parked on a spawn approval', () => {
  it('is not counted as running, and is counted as awaiting', () => {
    const { container } = chip({ parked: 1, running: 2 })
    expect(runningCount(container)).toBe('2')
    expect(awaitingCount(container)).toBe('1')
  })

  it('names the approval on its own row instead of leaving it blank', () => {
    const { container } = chip({ parked: 1 })
    const rows = container.querySelectorAll('[data-testid="subagent-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain(PARKED_LABEL)
  })

  it('keeps the chip mounted when the parked run is the whole wave', () => {
    // Regression guard for the fix itself: `running` no longer includes the
    // parked run, so a mount predicate of `running > 0 || queued > 0` would
    // unmount the one surface naming what the wave is blocked on.
    const { container } = chip({ parked: 1 })
    expect(runningCount(container)).toBe('0')
    expect(container.querySelector('[data-testid="subagent-histogram"]')).not.toBeNull()
  })

  it('reports no awaiting count when nothing is parked', () => {
    const { container } = chip({ running: 1 })
    expect(runningCount(container)).toBe('1')
    expect(container.querySelector('[data-testid="subagent-awaiting-count"]')).toBeNull()
  })

  it('prefers the approval over a stall verdict on the same run', () => {
    // The reaper measures an ABSENCE of stream events, which a run that never
    // started produces trivially. Naming the approval is strictly more specific,
    // and it is the only one of the two the user can act on.
    const { container } = chip({
      parked: 1,
      seed: d => d(sseSubagentStalled({ slot: SLOT, id: 'p0', stalled: true, idle_secs: 300 })),
    })
    const row = container.querySelector('[data-testid="subagent-row"]')
    expect(row?.textContent).toContain(PARKED_LABEL)
    expect(row?.textContent).not.toContain('possibly stalled')
  })

  it('leaves a pending entry with no approval_id in the running count', () => {
    // `approval_id` is the discriminator: without one, nothing proves the entry
    // is blocked on the user, so it must keep its previous treatment rather than
    // be reported under a state the user is being asked to resolve.
    const { container } = chip({
      running: 1,
      seed: d => d(sseSubagentPending({ slot: SLOT, id: 'x1', task: 'no approval id', approval_id: '' })),
    })
    expect(runningCount(container)).toBe('2')
    expect(container.querySelector('[data-testid="subagent-awaiting-count"]')).toBeNull()
  })
})

/**
 * A resolve that came back terminal (404 / 400 "no pending approval") records the
 * approval as gone. The composer and the activity panel both withdraw its
 * buttons, so no other surface may keep telling the user they owe that decision
 * -- but another surface may already have launched it. The chip therefore keeps
 * liveness unresolved (neither awaiting nor running) until reconciliation.
 */
describe('subagent whose spawn approval is gone', () => {
  const goneSeed = (d: (a: unknown) => void) => d(markSubagentApprovalGone({ id: 'p0', approval_id: 'spawn:p0' }))

  it('is neither awaiting nor running, and its row no longer asks for approval', () => {
    const { container } = chip({ parked: 1, running: 1, seed: goneSeed })
    expect(runningCount(container)).toBe('1')
    expect(container.querySelector('[data-testid="subagent-awaiting-count"]')).toBeNull()
    expect(unresolvedCount(container)).toBe('1')
    expect(container.textContent).not.toContain(PARKED_LABEL)
  })

  it('states liveness as neutral status and leaves the refusal to the surface that sent it', () => {
    const { container } = chip({ parked: 2, seed: goneSeed })
    // The refused request already reported itself where it was pressed (the
    // composer or the activity card); a second copy here would show the same
    // red sentence on two surfaces at once.
    expect(container.textContent).not.toContain(GONE_ERROR)
    expect(container.querySelector('[role="alert"]')).toBeNull()
    // The open liveness question: neutral, on the row and the count.
    const rows = container.querySelectorAll('[data-testid="subagent-row-checking"]')
    expect(rows).toHaveLength(1)
    expect(text(rows[0])).toBe(CHECKING)
    expect(container.querySelector('[data-testid="subagent-unresolved-count"]')!.getAttribute('title')).toBe(CHECKING)
  })

  it('drops the unresolved state once a real spawn and tool frame arrive', () => {
    const { container } = chip({
      parked: 1,
      seed: d => {
        goneSeed(d)
        d(sseSubagentSpawn({ slot: SLOT, id: 'p0', task: 'launched elsewhere', agent: 'kirocrew' }))
        d(sseSubagentTool({ slot: SLOT, id: 'p0', tool: 'shell' }))
      },
    })
    expect(runningCount(container)).toBe('1')
    expect(container.querySelector('[data-testid="subagent-unresolved-count"]')).toBeNull()
    expect(container.querySelector('[data-testid="subagent-row-checking"]')).toBeNull()
    expect(container.textContent).toContain('→ shell')
  })

  it('leaves the sidebar approval count', () => {
    const { store } = chip({ parked: 2, seed: goneSeed })
    expect(selectSidebarApprovalCounts(store.getState())).toEqual({ [SLOT]: 1 })
  })

  it('counts again once the card carries a fresh approval', () => {
    const { container } = chip({
      parked: 1,
      seed: d => {
        goneSeed(d)
        d(sseSubagentPending({ slot: SLOT, id: 'p0', task: 'parked 0', approval_id: 'spawn:p0:2' }))
      },
    })
    expect(awaitingCount(container)).toBe('1')
    expect(container.textContent).toContain(PARKED_LABEL)
  })
})

/**
 * The 30s poll is what settles a gone approval's liveness, and until it does the
 * composer and Reload stay blocked on the card. A refused read used to land in a
 * silent catch, so the chip showed only the neutral "Checking whether it
 * started…" forever. It is now reported through ErrorNotice, cleared by the next
 * successful read, and never taken as evidence that the agent ran or did not.
 */
describe('liveness poll for a gone spawn approval', () => {
  const LIVENESS_FAILED = "Couldn't check whether it started. Retrying…"
  const goneSeed = (d: (a: unknown) => void) => d(markSubagentApprovalGone({ id: 'p0', approval_id: 'spawn:p0' }))
  const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('reports a failed read through ErrorNotice and keeps the card unresolved', async () => {
    vi.mocked(api.spawnList).mockRejectedValueOnce(new Error('503'))
    const { container, store } = chip({ parked: 1, seed: goneSeed })
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).toBeNull()

    await tick()

    const notice = container.querySelector('[data-testid="subagent-liveness-error"]')
    expect(notice).not.toBeNull()
    expect(notice!.getAttribute('role')).toBe('alert')
    expect(text(notice)).toContain(LIVENESS_FAILED)
    // Neutral liveness: still unresolved, neither retired nor running.
    expect(unresolvedCount(container)).toBe('1')
    expect(text(container.querySelector('[data-testid="subagent-row-checking"]'))).toBe(CHECKING)
    expect(store.getState().chat.subagents.p0.status).toBe('pending')
    expect(store.getState().chat.subagents.p0.approvalGone).toBe('spawn:p0')
  })

  it('clears the failure once a later read succeeds', async () => {
    vi.mocked(api.spawnList)
      .mockRejectedValueOnce(new Error('503'))
      // Still parked server-side: the read succeeds without settling the card,
      // so only the transient failure may go away.
      .mockResolvedValueOnce({ agents: [{ id: 'p0', parent: `dashboard:${SLOT}`, awaiting_approval: true }] })
    const { container } = chip({ parked: 1, seed: goneSeed })

    await tick()
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).not.toBeNull()

    await tick()
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).toBeNull()
    expect(unresolvedCount(container)).toBe('1')
    expect(text(container.querySelector('[data-testid="subagent-row-checking"]'))).toBe(CHECKING)
  })

  it('forgets the failure once no gone approval is left, however it settled', async () => {
    vi.mocked(api.spawnList).mockRejectedValueOnce(new Error('503'))
    const { container, store } = chip({ parked: 2, running: 1, seed: goneSeed })
    await tick()
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).not.toBeNull()

    // Settled by a spawn frame, not by the poll: the poll never gets to clear it.
    await act(async () => {
      store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'p0', task: 'launched elsewhere', agent: 'kirocrew' }))
      store.dispatch(sseSubagentTool({ slot: SLOT, id: 'p0', tool: 'shell' }))
    })
    expect(unresolvedCount(container)).toBe('')
    // A second refusal: nothing has failed to read for p1 yet.
    await act(async () => { store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'spawn:p1' })) })
    expect(unresolvedCount(container)).toBe('1')
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).toBeNull()
  })

  it('stays silent when the failed poll had no gone approval to settle', async () => {
    vi.mocked(api.spawnList).mockRejectedValueOnce(new Error('503'))
    const { container } = chip({ running: 1 })
    await tick()
    expect(container.querySelector('[data-testid="subagent-liveness-error"]')).toBeNull()
  })
})

/**
 * The inventory row behind a gone approval is matched by run id: a nested,
 * cron- or channel-born run's parent is not the tab key. A card the inventory
 * retires was never launched, so nobody stopped it.
 */
describe('settling a gone spawn approval from the inventory', () => {
  const goneSeed = (d: (a: unknown) => void) => d(markSubagentApprovalGone({ id: 'p0', approval_id: 'spawn:p0' }))
  const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it.each([
    ['a nested child', 'subagent:root0001'],
    ['a cron-born run', 'cron:abc'],
  ])('finds %s by run id, so a launched run is not retired', async (_label, parent) => {
    vi.mocked(api.spawnList).mockResolvedValueOnce({
      agents: [{ id: 'p0', task: 'launched', done: false, parent }],
    })
    const { store } = chip({ parked: 1, seed: goneSeed })
    await tick()
    expect(store.getState().chat.subagents.p0.status).toBe('running')
  })

  it.each([
    ['a nested child', 'subagent:root0001'],
    ['a cron-born run', 'cron:abc'],
  ])('keeps %s it settled as running on the next poll, not failed', async (_label, parent) => {
    // Settled to running by run id; the following phantom sweep must match the
    // same live row by id too, or the card reads failed and "Dismiss done"
    // would DELETE (cancel) the still-running task.
    const live = { agents: [{ id: 'p0', task: 'launched', done: false, parent }] }
    vi.mocked(api.spawnList).mockResolvedValueOnce(live).mockResolvedValueOnce(live)
    const { store } = chip({ parked: 1, seed: goneSeed })
    await tick()
    expect(store.getState().chat.subagents.p0.status).toBe('running')
    await tick()
    expect(api.spawnList).toHaveBeenCalledTimes(2)
    expect(store.getState().chat.subagents.p0.status).toBe('running')
    expect(store.getState().chat.subagents.p0.error).toBeUndefined()
  })

  it('does not tally a retired never-launched card as stopped', async () => {
    // r0 stays live, so the chip stays mounted; p0 has no record and retires.
    vi.mocked(api.spawnList).mockResolvedValueOnce({
      agents: [{ id: 'r0', task: 'running 0', done: false, parent: `dashboard:${SLOT}` }],
    })
    const { container, store } = chip({ parked: 1, running: 1, seed: goneSeed })
    await tick()
    expect(store.getState().chat.subagents.p0.status).toBe('stopped')
    expect(store.getState().chat.subagents.p0.approvalRetired).toBe(true)
    expect(container.querySelector('[data-testid="subagent-stopped-count"]')).toBeNull()
  })
})

describe('the inventory read behind several gone approvals', () => {
  it('is one GET /api/spawn for every card that asks while it is in flight', async () => {
    let answer!: (v: unknown) => void
    vi.mocked(api.spawnList).mockReturnValueOnce(new Promise(resolve => { answer = resolve }) as never)
    const { store } = chip({
      parked: 3,
      seed: d => { for (const i of [0, 1, 2]) d(markSubagentApprovalGone({ id: `p${i}`, approval_id: `spawn:p${i}` })) },
    })
    // A batch refusal (or a reconnect) reconciles every gone card at once.
    const pending = [0, 1, 2].map(i => store.dispatch(
      reconcileGoneSubagent({ slot: SLOT, id: `p${i}`, approval_id: `spawn:p${i}` }) as never,
    ))
    answer({ agents: [{ id: 'p1', task: 'parked 1', done: false, parent: `dashboard:${SLOT}` }] })
    await act(async () => { await Promise.all(pending) })
    expect(api.spawnList).toHaveBeenCalledTimes(1)
    const subs = store.getState().chat.subagents
    expect([subs.p0.status, subs.p1.status, subs.p2.status]).toEqual(['stopped', 'running', 'stopped'])
  })
})
