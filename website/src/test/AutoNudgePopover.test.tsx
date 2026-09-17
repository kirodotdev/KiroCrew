import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { STOP_FILE_TOKEN, type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { __resetForTests, loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-1-100'

function renderPopover(loop: AutoNudgeLoop | null) {
  // A FRESH client per render: the popover reads the shared `cron-jobs` key, and
  // a client reused across tests would serve one test's stubbed rows to the next.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        slotKey={SLOT}
        loop={loop}
        open={true}
        onOpenChange={() => {}}
        onChange={() => {}}
      />
    </QueryClientProvider>,
  )
}

const makeLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'active loop goal',
  idle_secs: 90, max_cycles: 3, cycle_count: 1, active: true, last_fire_ts: 0,
  next_due_ts: 0, runtime_budget_spent: false, ...over,
})

describe('AutoNudgePopover goal persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    // The popover fetches on OPEN (reads /api/crons to list this slot's
    // watches) and on Save/Stop. Stub so nothing escapes the test.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('remembers the user-typed goal and restores it after the loop is gone (the reported bug)', () => {
    vi.useFakeTimers()
    // 1. User opens the popover (no loop yet) and types a custom goal.
    const first = renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'Ship the BYOA gate harness' } })
    // Debounced: not written synchronously. Advancing past the debounce persists it.
    expect(loadGoalDraft(SLOT)).toBeNull()
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    expect(loadGoalDraft(SLOT)?.message).toBe('Ship the BYOA gate harness')
    first.unmount()

    // 2. The loop is stopped elsewhere → ChatPage passes loop={null} on re-open;
    //    the popover restores the stored draft, not the default template.
    renderPopover(null)
    expect(goalBox().value).toBe('Ship the BYOA gate harness')
  })

  it('flushes a pending debounced edit on unmount (a fast close does not lose the last keystrokes)', () => {
    vi.useFakeTimers()
    const view = renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'closing fast' } })
    // Close BEFORE the debounce fires — the unmount flush must still persist it.
    expect(loadGoalDraft(SLOT)).toBeNull()
    view.unmount()
    expect(loadGoalDraft(SLOT)?.message).toBe('closing fast')
  })

  it('does not persist the pristine default (an untouched popover pins nothing, on open or close)', () => {
    vi.useFakeTimers()
    const view = renderPopover(null)
    // Opened, never edited → the edit-guard means no write, on debounce OR unmount.
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    expect(loadGoalDraft(SLOT)).toBeNull()
    view.unmount()
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('opening with an existing stored draft does not rewrite it (a mere view must not touch the store)', () => {
    // Seed a draft, snapshot the raw storage, then open (no edit) and close.
    // The stored bytes must be identical — no TTL refresh, no LRU bump.
    saveGoalDraft(SLOT, { message: 'remembered goal', idleSecs: 120, maxCycles: 5 })
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')

    const view = renderPopover(null)
    expect(goalBox().value).toBe('remembered goal') // restored on open
    view.unmount() // close without editing

    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)
  })

  it('prefers the live loop message over a stored draft when a loop is running', () => {
    saveGoalDraft(SLOT, { message: 'stale draft goal', idleSecs: 60, maxCycles: 0 })
    renderPopover(makeLoop({ message: 'active loop goal' }))
    expect(goalBox().value).toBe('active loop goal')
  })

  it('opening with a live loop never writes the loop config into the draft store', () => {
    vi.useFakeTimers()
    // No stored draft. Open with a live loop, let any timer fire, then close.
    const view = renderPopover(makeLoop())
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    view.unmount()
    // The live loop's config must NOT have been mirrored into the user-draft store.
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('editing while a loop is running does not persist to the draft store (loop is authoritative)', () => {
    vi.useFakeTimers()
    const view = renderPopover(makeLoop())
    fireEvent.change(goalBox(), { target: { value: 'tweaked while running' } })
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    view.unmount()
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('falsy loop fields fall back to default template / 60 / 0, not bare "" / 0 (|| not ??)', () => {
    // A loop with an empty message and idle_secs/max_cycles of 0 must show the
    // default template + 60 — falsy loop fields fall back (|| not ??).
    renderPopover(makeLoop({ message: '', idle_secs: 0, max_cycles: 0 }))
    expect(goalBox().value).toContain('north star')
    expect((screen.getByDisplayValue('60') as HTMLInputElement).value).toBe('60')
  })
})

describe('AutoNudgePopover number-field editing (idle / max cycles)', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  // Idle is the first number input, max-cycles the second (DOM order in the JSX).
  const fields = () => screen.getAllByRole('spinbutton') as HTMLInputElement[]
  const idleField = () => fields()[0]
  const cyclesField = () => fields()[1]
  const goalBox = () => screen.getByRole('textbox', { name: 'Goal description' }) as HTMLTextAreaElement

  it('allows clearing the idle field to empty while typing, then defaults to 60 on blur (the reported bug)', () => {
    renderPopover(null)
    expect(idleField().value).toBe('60')
    // The empty edit is allowed as-typed rather than snapping straight back to
    // 60 with the leading digit stuck...
    fireEvent.change(idleField(), { target: { value: '' } })
    expect(idleField().value).toBe('')
    // ...and only commits to the default when the field loses focus.
    fireEvent.blur(idleField())
    expect(idleField().value).toBe('60')
  })

  it('retypes idle 60 -> 30 without the leading digit sticking', () => {
    renderPopover(null)
    fireEvent.change(idleField(), { target: { value: '' } })
    fireEvent.change(idleField(), { target: { value: '30' } })
    expect(idleField().value).toBe('30')
    fireEvent.blur(idleField())
    expect(idleField().value).toBe('30')
  })

  it('empty max-cycles commits to 0 (infinity) on blur', () => {
    renderPopover(null)
    expect(cyclesField().value).toBe('0')
    fireEvent.change(cyclesField(), { target: { value: '' } })
    expect(cyclesField().value).toBe('')
    fireEvent.blur(cyclesField())
    expect(cyclesField().value).toBe('0')
  })

  it('Save sends the typed idle value even without an intervening blur', async () => {
    renderPopover(null)
    fireEvent.change(idleField(), { target: { value: '45' } })
    // Click Start loop WITHOUT blurring the field first — save() must read the
    // raw string, not a stale committed number.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })
    // Select the call by URL, not by index: opening the popover also READS
    // /api/crons to list this slot's watches, so the save POST is no longer
    // call 0 and an index would pin an unrelated ordering.
    // The init arg is optional and its `body` is too: the /api/crons read is a
    // bare `fetch(url)` and a delete carries only `{ method }`, so `c[1]?.body`
    // below is load-bearing rather than defensive.
    const calls = (fetch as unknown as { mock: { calls: [string, { body?: string }?][] } }).mock.calls
    const save = calls.find(c => String(c[0]).startsWith('/api/autonudge') && c[1]?.body)
    expect(save, 'no /api/autonudge write was issued').toBeTruthy()
    const body = JSON.parse(save![1]!.body!)
    expect(body.idle_secs).toBe(45)
  })

  it('withholds Save on a runtime-budget-stopped goal: the clear the help names is its only exit', async () => {
    renderPopover(makeLoop({
      active: false,
      next_due_ts: 0,
      stopped_reason: 'runtime_budget',
      runtime_budget_spent: true,
    }))

    expect(screen.getByTestId('auto-nudge-loop-paused').textContent)
      .toBe('Stopped · Reached its maximum runtime.')
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Clear stopped goal, then start a new goal.')
    // The popover carries no runtime field, so nothing it could PATCH revives
    // this record: a Save here edited a goal that can never run again, beside a
    // help line saying the only step left is to clear it.
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Start loop/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeEnabled()
    // Readable, not dead: "start a new goal" needs the old text, and a confirmed
    // clear closes the popover without copying the erased record into the draft.
    expect(goalBox().readOnly).toBe(true)
    expect(goalBox().disabled).toBe(false)
    expect(goalBox().value).toBe('active loop goal')
    expect(idleField().readOnly).toBe(true)
    expect(cyclesField().readOnly).toBe(true)

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    expect(calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')).toBeUndefined()
  })

  it('keeps Stop loop and Save on a loop that is still active when its runtime budget flips', () => {
    // The budget is checked at serialization, the terminal stop lands on the
    // timer's next tick: between them the record reads active with a spent
    // budget. The active shape is authoritative for that window -- Stop must
    // stay reachable, and the row must not retire a goal the service still runs.
    renderPopover(makeLoop({ active: true, runtime_budget_spent: true }))

    expect(screen.getByRole('button', { name: /Stop loop/i })).toBeEnabled()
    expect(screen.getByRole('button', { name: /^Save$/i })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
    expect(goalBox().readOnly).toBe(false)
  })

  it('keeps the retired goal readable when its clear fails', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: { method?: string }) => Promise.resolve(
      init?.method === 'DELETE'
        ? { ok: false, status: 500, json: () => Promise.resolve({ error: 'store unavailable' }) }
        : { ok: true, json: () => Promise.resolve({ loop: null }) },
    )) as unknown as typeof fetch)
    renderPopover(makeLoop({
      active: false,
      next_due_ts: 0,
      stopped_reason: 'runtime_budget',
      runtime_budget_spent: true,
    }))

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })

    expect(screen.getByTestId('auto-nudge-error').textContent).toContain('store unavailable')
    // A failed erase leaves the record where it was: still retired, still
    // copyable, still without a Save that would pretend otherwise, and still
    // holding the primed confirmation so the user can retry or back out.
    expect(goalBox().value).toBe('active loop goal')
    expect(goalBox().readOnly).toBe(true)
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Clear goal for good' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('uses a non-repeating approval-stalled suffix', () => {
    renderPopover(makeLoop({
      active: false,
      next_due_ts: 0,
      stopped_reason: 'approval_stalled',
    }))

    expect(screen.getByTestId('auto-nudge-loop-paused').textContent)
      .toBe('Stopped · Waiting for a tool approval in this conversation.')
  })

  it('restarts after another caller lifts a stale runtime budget', async () => {
    renderPopover(makeLoop({
      active: false,
      next_due_ts: 0,
      stopped_reason: 'runtime_budget',
      runtime_budget_spent: false,
    }))

    expect(screen.getByTestId('auto-nudge-loop-paused').textContent)
      .toBe('Stopped')
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Start loop resumes this goal. Clear stopped goal removes it for good.')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no restart PATCH for the unspent goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toHaveProperty('active', true)
  })

  it('restarts a reasonless legacy stop when its bounds still allow work', async () => {
    renderPopover(makeLoop({ active: false, next_due_ts: 0, stopped_reason: '' }))

    expect(screen.getByRole('button', { name: /Start loop/i })).toBeTruthy()
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Start loop resumes this goal. Clear stopped goal removes it for good.')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no restart PATCH for the legacy goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toHaveProperty('active', true)
  })

  it('shows a disabled Start loop on a cycle-capped goal and enables it once the cap is raised (the two-step revival)', async () => {
    renderPopover(makeLoop({ active: false, next_due_ts: 0, stopped_reason: 'cycle_cap', cycle_count: 3, max_cycles: 3 }))
    expect(screen.getByTestId('auto-nudge-loop-paused').textContent)
      .toBe('Stopped · Reached Max cycles.')
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Raise Max cycles, then press Start loop to resume this goal. Clear stopped goal removes it for good.')
    // The control the help names is on the surface BEFORE the field is raised
    // -- disabled, because the record cannot be revived yet -- and nothing in
    // the row reads Save: a Save here edited a goal that stayed stopped, beside
    // a line telling the reader to press a button they could not find.
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeEnabled()
    // A press on the disabled control issues nothing -- neither a restart nor
    // the configuration-only PATCH the old Save sent.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start loop' })) })
    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    expect(calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')).toBeUndefined()

    fireEvent.change(cyclesField(), { target: { value: '4' } })

    // Raising the cap ENABLES the button the copy told the user to press next;
    // the label does not change, only its state, and the help drops the first
    // step it no longer needs.
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Start loop resumes this goal. Clear stopped goal removes it for good.')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start loop' })) })

    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no restart PATCH for the capped goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toEqual({
      message: 'active loop goal',
      idle_secs: 90,
      max_cycles: 4,
      active: true,
    })
  })

  it('re-disables Start loop when the cap is lowered back onto the delivered count, and reads an emptied field as no cap', () => {
    renderPopover(makeLoop({ active: false, next_due_ts: 0, stopped_reason: 'cycle_cap', cycle_count: 3, max_cycles: 3 }))
    const startLoop = () => screen.getByRole('button', { name: 'Start loop' })
    expect(startLoop()).toBeDisabled()
    // An emptied field commits to 0 on blur, and 0 is "infinite": the cap is
    // lifted as-typed, so the button enables before the blur.
    fireEvent.change(cyclesField(), { target: { value: '' } })
    expect(startLoop()).toBeEnabled()
    // Back onto the delivered count blocks again; below it blocks too. The
    // state tracks the field on every edit, not only on the first raise.
    fireEvent.change(cyclesField(), { target: { value: '3' } })
    expect(startLoop()).toBeDisabled()
    fireEvent.change(cyclesField(), { target: { value: '2' } })
    expect(startLoop()).toBeDisabled()
    fireEvent.change(cyclesField(), { target: { value: '5' } })
    expect(startLoop()).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
  })

  it('keeps an ordinary, enabled Save on an ACTIVE loop whose field is at or below the delivered count', async () => {
    // The cap gates RESTART, not configuration: a running loop that has reached
    // its cap (the window between the last delivery and the timer's terminal
    // stop), or whose field the user lowers while it runs, still saves its
    // edits with the Save it always had -- not a disabled Start loop.
    renderPopover(makeLoop({ active: true, cycle_count: 3, max_cycles: 3 }))
    expect(screen.getByRole('button', { name: /^Save$/i })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Start loop' })).toBeNull()
    fireEvent.change(cyclesField(), { target: { value: '1' } })
    expect(screen.getByRole('button', { name: /^Save$/i })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Start loop' })).toBeNull()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Save$/i })) })

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no config PATCH for the active goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toEqual({ message: 'active loop goal', idle_secs: 90, max_cycles: 1 })
  })

  it('retires a cycle-capped loop whose runtime budget is also spent: raising the cap reveals nothing', async () => {
    renderPopover(makeLoop({
      active: false,
      next_due_ts: 0,
      stopped_reason: 'cycle_cap',
      cycle_count: 3,
      max_cycles: 4,
      runtime_budget_spent: true,
    }))

    // The runtime bound wins over the cycle cap for the help AND for the row:
    // the two-step revival the cycle-cap help describes has no second step
    // here, so neither Save nor Start loop -- enabled or disabled -- is offered.
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Clear stopped goal, then start a new goal.')
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Start loop/i })).toBeNull()
    expect(cyclesField().readOnly).toBe(true)
    // Even an edit that lifts the cap reveals nothing: the retired shape owns the
    // row, and the clear the help names is its only control.
    fireEvent.change(cyclesField(), { target: { value: '9' } })
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Start loop/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeEnabled()

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    expect(calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')).toBeUndefined()
  })

  it('holds an approval-stalled loop whose cycle cap is spent on a disabled Start loop until the cap is raised', async () => {
    renderPopover(makeLoop({ active: false, next_due_ts: 0, stopped_reason: 'approval_stalled', cycle_count: 3, max_cycles: 3 }))

    // The stall is the reason shown, but the cap is the bound that blocks the
    // restart, so the row takes the capped shape: the help names the two steps,
    // the button they name is present and disabled, and no Save offers a
    // configuration write that could read as a way back.
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Raise Max cycles, then press Start loop to resume this goal. Clear stopped goal removes it for good.')
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^Save$/i })).toBeNull()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start loop' })) })
    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    expect(calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')).toBeUndefined()

    fireEvent.change(cyclesField(), { target: { value: '4' } })
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeEnabled()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start loop' })) })
    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no restart PATCH for the approval-stalled goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toHaveProperty('active', true)
  })

  it('restarts an approval-stalled loop when its bounds still allow work', async () => {
    renderPopover(makeLoop({ active: false, next_due_ts: 0, stopped_reason: 'approval_stalled', cycle_count: 0, max_cycles: 0 }))

    expect(screen.getByRole('button', { name: /Start loop/i })).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const patch = calls.find(c => c[0] === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    expect(patch, 'no restart PATCH for the approval-stalled goal was issued').toBeTruthy()
    expect(JSON.parse(patch![1]!.body!)).toHaveProperty('active', true)
  })
})

describe('AutoNudgePopover trigger chip — interrupted state', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  const renderChip = (loop: AutoNudgeLoop | null, interrupted: boolean) => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <AutoNudgePopover
        slotKey={SLOT}
        loop={loop}
        open={false}
        onOpenChange={() => {}}
        onChange={() => {}}
        interrupted={interrupted}
      />
    </QueryClientProvider>,
  )

  it('pulses while the loop is active and the session is healthy', () => {
    renderChip(makeLoop({ cycle_count: 47 }), false)
    const chip = screen.getByTitle('Goal active (cycle 47/3)')
    expect(chip.className).toContain('animate-pulse')
    expect(chip.textContent).toContain('47')
  })

  it('stops pulsing and explains itself when the last turn was interrupted (the reported bug)', () => {
    // The composer is showing Resume: nothing runs until the user acts or the
    // next idle-timer cycle fires, so a pulsing chip would claim active work
    // for that whole gap.
    renderChip(makeLoop({ cycle_count: 47 }), true)
    const chip = screen.getByTitle(/last turn was interrupted/)
    expect(chip.className).not.toContain('animate-pulse')
    // The cycle count survives — it is state, not a liveness claim.
    expect(chip.textContent).toContain('47')
  })

  it('ignores interrupted when no loop is active (plain set-a-goal chip)', () => {
    renderChip(null, true)
    const chip = screen.getByTitle('Set a goal')
    expect(chip.className).not.toContain('animate-pulse')
  })
})


describe('AutoNudgePopover — zero-token watches armed on this slot', () => {
  const cron = (over: Record<string, unknown> = {}) => ({
    id: 'j1',
    name: 'pr watch #6234',
    schedule: 'every 60s',
    next_run_ts: 1787816571,
    session_key: `dashboard:${SLOT}`,
    script: '~/.kiro/crew/crons/pr_watch.py:watch',
    enabled: true,
    ...over,
  })

  function stubCrons(rows: unknown[]) {
    // `{ jobs: [...] }` is the endpoint's real envelope. An earlier version of
    // these tests stubbed a bare array, which matched a wrong reader and hid a
    // section that never rendered against the live gateway -- the fixture has to
    // be the shape the server sends, or the test only proves the reader agrees
    // with itself.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(String(url).startsWith('/api/crons') ? { jobs: rows } : { loop: null }),
        }),
      ) as unknown as typeof fetch,
    )
  }

  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.unstubAllGlobals() })

  /**
   * Render, then wait for the crons read to have been ANSWERED, not just issued.
   *
   * The section is populated by `fetch` -> `json()` -> `setState`, three promise
   * hops that `act` does not wait for, so a bare `await act(render)` samples the
   * popover before the answer lands. That made the positive test below flake
   * (1 in 5 full runs on a loaded host) and every "not listed" assertion in this
   * block vacuous: the section is absent BEFORE the fetch resolves whether or not
   * the filter works. Waiting on the mocked fetch having been called, then
   * draining the chain, makes both kinds of assertion about the rendered answer.
   */
  async function renderPopoverSettled() {
    await act(async () => { renderPopover(null) })
    const fetchMock = vi.mocked(fetch)
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(c => String(c[0]).startsWith('/api/crons'))).toBe(true),
    )
    for (let i = 0; i < 4; i++) {
      await act(async () => { await Promise.resolve() })
    }
  }

  it('lists a script cron this slot owns, so an armed watch is visible in chat', async () => {
    // The reported gap: a watch is deliberately NOT an autonudge loop, so the
    // popover showed "Set a goal" and nothing else while a watch was polling --
    // the one surface a user opens to confirm something is running.
    stubCrons([cron()])
    await renderPopoverSettled()
    expect(await screen.findByText(/Zero-token watches/i)).toBeTruthy()
    expect(screen.getByText('pr watch #6234')).toBeTruthy()
  })

  it('never lists a watch owned by a different slot', async () => {
    // Ownership goes through the shared `runBelongsToSlot`, which normalizes the
    // `dashboard:` namespace rather than demanding byte equality -- but the SLOT
    // must still match, and that is the property worth pinning: another
    // conversation's watch appearing here is worse than showing none.
    stubCrons([cron({ session_key: 'dashboard:chat-9-999', name: 'someone elses watch' })])
    await renderPopoverSettled()
    expect(screen.queryByText('someone elses watch')).toBeNull()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('never lists a message-only cron under a zero-token heading', async () => {
    // A cron with no script wakes the agent every fire. Listing it here would
    // make the heading lie about what it costs.
    stubCrons([cron({ script: '', name: 'daily reminder' })])
    await renderPopoverSettled()
    expect(screen.queryByText('daily reminder')).toBeNull()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('never lists a disabled watch as if it were armed', async () => {
    stubCrons([cron({ enabled: false, name: 'paused watch' })])
    await renderPopoverSettled()
    expect(screen.queryByText('paused watch')).toBeNull()
  })

  it('reads the jobs envelope the endpoint actually returns, not a bare array', async () => {
    // The live endpoint answers `{ jobs: [...] }` (handlers/cron.py). Reading a
    // bare array fails SILENTLY -- no error, the filter just never matches -- so
    // this pins the envelope rather than trusting the reader. Found by a pod
    // capture after the unit tests were green against the wrong fixture.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(String(url).startsWith('/api/crons') ? [cron()] : { loop: null }),
        }),
      ) as unknown as typeof fetch,
    )
    await renderPopoverSettled()
    // A bare array is NOT the contract, so nothing should be read out of it.
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('stays silent when the read fails rather than banner-ing over the goal form', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        String(url).startsWith('/api/crons')
          ? Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) })
          : Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) }),
      ) as unknown as typeof fetch,
    )
    await renderPopoverSettled()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
    // The popover's actual job is still fully usable.
    expect(screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i)).toBeTruthy()
  })
})

/** #6482: hovering the goal button / opening the popover shows a live countdown
 *  to the next trigger, computed from the loop's already-serialized next_due_ts. */
describe('AutoNudgePopover next-trigger countdown', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
    vi.useFakeTimers()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const nowSecs = () => Date.now() / 1000

  it('shows the countdown in the popover and the trigger tooltip, and it ticks', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 125 }))
    // 125s -> "2m 5s" (en narrow units via fmtDuration).
    expect(screen.getAllByText(/Next cycle in .*2.*m.*5.*s/i).length).toBeGreaterThan(0)
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)

    // One tick: the rendered remaining time decreases.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getAllByText(/Next cycle in .*2.*m.*4.*s/i).length).toBeGreaterThan(0)
  })

  it('drops the seconds digit above an hour', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 3_720 }))
    const line = screen.getAllByText(/Next cycle in/i)[0].textContent || ''
    expect(line).toMatch(/1.*h/i)
    expect(line).not.toMatch(/\ds\b/)
  })

  it('reads "due" instead of a negative countdown when the deadline elapsed mid-turn', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() - 5 }))
    expect(screen.getAllByText(/Next cycle due, fires after the current turn/i).length).toBeGreaterThan(0)
  })

  it('shows the unscheduled placeholder when next_due_ts is 0', () => {
    renderPopover(makeLoop({ next_due_ts: 0 }))
    expect(screen.getAllByText(/Next cycle not yet scheduled/i).length).toBeGreaterThan(0)
  })

  it('shows no countdown for an inactive loop', () => {
    renderPopover(makeLoop({ active: false, next_due_ts: nowSecs() + 300 }))
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
  })

  /** Review finding: the countdown must stay OUT of aria-label — a per-second
   *  label change re-announces the button to screen readers. Title only. */
  it('keeps aria-label stable (countdown lives in title only)', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 125 }))
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('aria-label')).not.toMatch(/Next cycle/i)
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)
  })

  /** Review finding: the 1s ticker is popover-open-only — a closed-but-armed
   *  loop must not re-render the toolbar button every second. Hover/focus
   *  refresh the snapshot instead, which is all a native tooltip can show. */
  it('does not tick while closed; hovering the trigger refreshes the tooltip', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const deadline = nowSecs() + 125
    render(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover slotKey={SLOT} loop={makeLoop({ next_due_ts: deadline })} open={false} onOpenChange={() => {}} onChange={() => {}} />
      </QueryClientProvider>,
    )
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('title')).toMatch(/2.*m.*5.*s/i)

    // A minute passes with the popover closed: no interval is armed, so the
    // title still carries the mount-time snapshot...
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(trigger.getAttribute('title')).toMatch(/2.*m.*5.*s/i)

    // ...until a hover refreshes it to the current remaining time.
    fireEvent.mouseEnter(trigger)
    expect(trigger.getAttribute('title')).toMatch(/1.*m.*5.*s/i)
  })

  it('stops updating after the loop goes inactive (ticker torn down)', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const deadline = nowSecs() + 125
    const props = { slotKey: SLOT, open: true, onOpenChange: () => {}, onChange: () => {} }
    const view = render(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover {...props} loop={makeLoop({ next_due_ts: deadline })} />
      </QueryClientProvider>,
    )
    expect(screen.getAllByText(/Next cycle in/i).length).toBeGreaterThan(0)

    view.rerender(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover {...props} loop={makeLoop({ active: false, next_due_ts: deadline })} />
      </QueryClientProvider>,
    )
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
    // Advancing the clock after teardown must not resurrect it or throw.
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
  })
})

/** #7410 residual 1: the cycle readout carries its cap, so a loop coasting
 *  toward its max_cycles backstop is visible before it silently stops. */
describe('AutoNudgePopover cycle cap readout', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const renderChip = (loop: AutoNudgeLoop | null, interrupted = false) => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <AutoNudgePopover
        slotKey={SLOT}
        loop={loop}
        open={false}
        onOpenChange={() => {}}
        onChange={() => {}}
        interrupted={interrupted}
      />
    </QueryClientProvider>,
  )

  it('renders the cap beside the cycle number, so a loop nearing its backstop is visible before it stops', () => {
    // The reported gap: max_cycles reached the frontend but was never displayed,
    // so cycle 23 of 24 looked exactly like cycle 23 of an uncapped loop.
    renderChip(makeLoop({ cycle_count: 23, max_cycles: 24 }))
    const chip = screen.getByTitle('Goal active (cycle 23/24)')
    expect(chip.textContent).toContain('23/24')
    // Screen-reader users learn the cap too — it is state, not a live countdown.
    expect(chip.getAttribute('aria-label')).toBe('Goal active (cycle 23/24)')
  })

  it('renders a bare cycle count with no slash when max_cycles is 0, because an uncapped loop has no denominator to count toward', () => {
    renderChip(makeLoop({ cycle_count: 23, max_cycles: 0 }))
    const chip = screen.getByTitle('Goal active (cycle 23)')
    expect(chip.textContent).toContain('23')
    expect(chip.textContent).not.toContain('/')
    expect(chip.getAttribute('aria-label')).toBe('Goal active (cycle 23)')
  })

  it('carries the cap into the interrupted tooltip too, since an interrupted loop is still armed against that cap', () => {
    renderChip(makeLoop({ cycle_count: 12, max_cycles: 24 }), true)
    const chip = screen.getByTitle(/last turn was interrupted/)
    expect(chip.getAttribute('title')).toContain('cycle 12/24')
  })

  it('shows the capped readout in the popover header, not only on the chip', () => {
    renderPopover(makeLoop({ cycle_count: 3, max_cycles: 24 }))
    expect(screen.getByText('· cycle 3/24')).toBeTruthy()
  })

  it('keeps the capped aria-label static while the countdown ticks (a cap must not re-announce the button every second)', () => {
    // Pins the same contract as "keeps aria-label stable": the cap is derived
    // from cycle_count/max_cycles only, so an armed ticker changes the title and
    // leaves the label alone.
    vi.useFakeTimers()
    renderPopover(makeLoop({ cycle_count: 3, max_cycles: 24, next_due_ts: Date.now() / 1000 + 125 }))
    const trigger = screen.getByRole('button', { name: 'Goal active (cycle 3/24)' })
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)
    act(() => { vi.advanceTimersByTime(3_000) })
    expect(trigger.getAttribute('aria-label')).toBe('Goal active (cycle 3/24)')
    expect(trigger.getAttribute('aria-label')).not.toMatch(/Next cycle/i)
  })
})

describe('AutoNudgePopover Trigger nudge (#8212)', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  /** Local render helper: the shared one hardcodes no-op callbacks, and these
   *  tests are about what the press DOES to them.
   *
   *  `open` is CONTROLLED here, mirroring the real parent (ChatInput owns the
   *  flag and feeds it back). A fixed `open={true}` would make the harness pin
   *  the popover's presence, so "the edit survives" could not fail even if the
   *  code closed it -- the assertion would be about the fixture rather than the
   *  component. */
  const renderWith = (loop: AutoNudgeLoop | null, onChange = vi.fn()) => {
    const onOpenChange = vi.fn()
    const Harness = () => {
      const [open, setOpen] = useState(true)
      return (
        <AutoNudgePopover
          slotKey={SLOT}
          loop={loop}
          open={open}
          onOpenChange={v => { onOpenChange(v); setOpen(v) }}
          onChange={onChange}
        />
      )
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    )
    return { onChange, onOpenChange }
  }

  const triggerButton = () => screen.queryByRole('button', { name: 'Trigger nudge' })

  it('offers the button while a loop is active', () => {
    renderWith(makeLoop())
    expect(triggerButton()).toBeTruthy()
  })

  it('offers it NOWHERE when no loop is running, so the affordance never appears without a subject', () => {
    renderWith(null)
    // Complement assertion rather than a bare negative on one node: a stale
    // render could leave the button somewhere else in the tree, and "the
    // button I looked for is absent" would still pass.
    expect(triggerButton()).toBeNull()
    expect(screen.queryAllByRole('button', { name: /Trigger/i })).toHaveLength(0)
  })

  it('offers it NOWHERE for a paused loop, because the server refuses to fire one', () => {
    // Gated on `active`, not on `loop`: every terminal bound leaves the loop
    // inactive, so a button here could only ever produce a 409.
    renderWith(makeLoop({ active: false }))
    expect(triggerButton()).toBeNull()
    expect(screen.queryAllByRole('button', { name: /Trigger/i })).toHaveLength(0)
  })

  it('disables itself once a cycle is due, so a press visibly acknowledges itself', () => {
    // The press used to leave the button re-enabled and unchanged, so a reader
    // could not tell whether pressing again would double the nudge. It would not:
    // the cycle is already armed. Both directions asserted -- a loop that is NOT
    // due must stay pressable, or this would disable the feature it guards.
    renderWith(makeLoop({ next_due_ts: 1_700_000_000 }))
    expect(triggerButton()).toBeTruthy()
    expect((triggerButton() as HTMLButtonElement).disabled).toBe(true)
    cleanup()
    renderWith(makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 300 }))
    expect((triggerButton() as HTMLButtonElement).disabled).toBe(false)
  })

  it('names the way OUT of a manually paused loop instead of leaving Save to do it silently', () => {
    // A manual pause has no terminal bound to lift, so the primary button may
    // explicitly restart it. Reasonless legacy stops stay config-only because
    // the client cannot prove why they became inactive.
    renderWith(makeLoop({ active: false, stopped_reason: 'manual' }))
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Start loop' })).toBeNull()
  })

  it('says the loop is stopped where the button would be, so the absence has a reason', () => {
    // Absence alone is ambiguous: an inactive loop looked identical to an active
    // one whose button failed to render, and a usability reader could not tell
    // the stopped screenshot was even the same loop. The state is the reason for
    // the absence, so it occupies the space the absence leaves.
    renderWith(makeLoop({ active: false }))
    expect(screen.getByTestId('auto-nudge-loop-paused')).toBeTruthy()
    // And it is genuinely conditional, not always-on decoration.
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.queryByTestId('auto-nudge-loop-paused')).toBeNull()
    expect(triggerButton()).toBeTruthy()
  })

  it('posts to the loop-scoped fire route with NO body, so the ARMED message is what fires', async () => {
    const fired = makeLoop({ next_due_ts: 1_700_000_000 })
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).endsWith('/fire') ? { ok: true, loop: fired } : { loop: null }),
      }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())

    await act(async () => { fireEvent.click(triggerButton()!) })

    // Selected by URL, not by index: opening the popover also reads /api/crons.
    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const fire = calls.find(c => String(c[0]) === '/api/autonudge/l1/fire')
    expect(fire, 'no POST to the fire route was issued').toBeTruthy()
    expect(fire![1]?.method).toBe('POST')
    // Load-bearing: a body would let a stale popover field become the prompt.
    // The nudge fired must be whatever the loop currently holds, read server-side.
    expect(fire![1]?.body).toBeUndefined()
    // The server no longer moves the deadline, so the component supplies the
    // armed one. Asserted field-wise rather than by identity: the loop's own
    // data must be passed through untouched, and only `next_due_ts` replaced.
    const passed = onChange.mock.calls.at(-1)?.[0]
    expect(passed).toMatchObject({ ...fired, next_due_ts: expect.any(Number) })
    expect(passed.next_due_ts).toBeGreaterThan(Date.now() / 1000 - 5)
    // And it must NOT close: closing would drop an unsaved edit in the textarea
    // 40px above, with no dirty guard, so a press after an edit would cost the
    // user their text on top of spending a turn on the old prompt.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('keeps a typed-but-unsaved goal edit after a successful press', async () => {
    // The complement of the assertion above, stated as the user-visible fact
    // rather than as a callback that was not invoked: a press must never be a
    // silent way to lose work.
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).endsWith('/fire') ? { ok: true, loop: makeLoop() } : { loop: null }),
      }),
    ) as unknown as typeof fetch)
    renderWith(makeLoop())
    const box = screen.getByLabelText('Goal description') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'edited but not saved' } })

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect((screen.getByLabelText('Goal description') as HTMLTextAreaElement).value)
      .toBe('edited but not saved')
  })

  it('surfaces a refusal inline and keeps the popover open, because it holds unsaved fields', async () => {
    // The refusal names the outcome and the next step, not just the condition:
    // a reader must be able to tell a refusal from a delay, and the press was
    // refused rather than queued.
    const REFUSAL = 'nudge not sent: the agent is still working, so try again when it finishes'
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      String(url).endsWith('/fire')
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: REFUSAL, code: 'session_busy' }) })
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect(screen.getByText(REFUSAL)).toBeTruthy()
    // A refusal must not report success by tearing the popover down.
    expect(onChange).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('names the object it clears once the loop is already stopped, and says the erase is final', async () => {
    // One button, two actions. On a live loop the press stops the loop and keeps
    // the record. On a stopped one there is nothing left to stop: the press
    // removes it, which is the only way the slot can watch something else -- a
    // stopped structured monitor blocks a re-arm until its row is gone.
    // "Clear record" failed a blind read (the popover shows nothing called a
    // "record"), so the label names the GOAL, the status reads Stopped rather
    // than the resumable-sounding Paused, and a help line names both exits
    // because the erase has no undo. Both directions asserted so this cannot
    // just move the confusion.
    renderWith(makeLoop({ active: false, stopped_reason: 'manual' }))
    const clear = screen.getByRole('button', { name: 'Clear stopped goal' })
    expect(clear).toBeTruthy()
    // Danger-coloured unconditionally, not on :hover -- a touch viewport never
    // produces hover, so a hover-only colour renders an irreversible erase
    // identically to the buttons beside it.
    expect(clear.className).toContain('text-danger')
    expect(screen.queryByRole('button', { name: 'Stop loop' })).toBeNull()
    expect(screen.getByTestId('auto-nudge-loop-paused').textContent).toBe('Stopped')
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Start loop resumes this goal. Clear stopped goal removes it for good.')
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Stop loop' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
    expect(screen.queryByTestId('auto-nudge-stopped-help')).toBeNull()
  })

  it('asks before erasing a stopped goal, and each label restates the action', async () => {
    // Same two-step the monitor surface uses for its identical erase. The
    // confirm row renders no question, so a bare "Yes" would name nothing:
    // both labels have to restate what happens.
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') calls.push(String(url))
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
    }) as unknown as typeof fetch)

    renderWith(makeLoop({ active: false }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    expect(calls).toEqual([])
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    // Two controls, not three: the confirmation replaces the primary CTA rather
    // than sitting beside it (website/AUTOSDE.yaml:230 caps a row at two). Its
    // back-out reads "Cancel", the same word the monitor surface's confirm uses
    // for the same act.
    const row = screen.getByRole('button', { name: 'Cancel' }).parentElement!
    expect(Array.from(row.querySelectorAll('button')).map(b => b.textContent))
      .toEqual(['Cancel', 'Clear goal for good'])
    // And the help line becomes the question, instead of naming two buttons that
    // just left the row.
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Remove this goal for good?')
    // Cancelling erases nothing and restores the original control.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    expect(calls).toEqual([])
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeTruthy()
    // Second press through the confirm performs it.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })
    expect(calls).toEqual(['/api/autonudge/l1?intent=clear'])
  })

  it('drops a primed confirmation when the record changes under the popover', async () => {
    // The popover re-renders from websocket state without closing, so another
    // tab can swap the record while a confirmation is primed: edit and restart
    // the same loop id, then a cycle cap stops it again. The press would then
    // erase a goal the confirmation never described, and the server sees no
    // mismatch because the record is inactive both times. Each of the three
    // changes that can arrive this way is asserted.
    // A harness that can swap the loop WITHOUT closing the popover, which is
    // what a websocket-driven re-render does.
    const Swappable = ({ next }: { next: Partial<AutoNudgeLoop> }) => {
      const [loop, setLoop] = useState<AutoNudgeLoop>(makeLoop({ active: false }))
      return (
        <>
          <button onClick={() => setLoop(current => ({ ...current, ...next }))}>swap</button>
          <AutoNudgePopover
            slotKey={SLOT}
            loop={loop}
            open={true}
            onOpenChange={() => {}}
            onChange={() => {}}
          />
        </>
      )
    }

    for (const next of [
      { id: 'l2' },
      { active: true },
      { message: 'a different goal entirely' },
    ]) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
      render(
        <QueryClientProvider client={qc}>
          <Swappable next={next} />
        </QueryClientProvider>,
      )
      fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
      expect(screen.getByRole('button', { name: 'Clear goal for good' })).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'swap' }))
      expect(screen.queryByRole('button', { name: 'Clear goal for good' })).toBeNull()
      cleanup()
    }
  })

  it('sends the pressed INTENT so a stale label cannot erase a record it did not mean to', async () => {
    // The server otherwise reads the operation off the record's state at arrival
    // time, so a "Stop loop" press against a record that went terminal in the
    // meantime would clear it. The intent travels with the request; the server
    // 409s on a mismatch.
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') calls.push(String(url))
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
    }) as unknown as typeof fetch)

    renderWith(makeLoop({ active: true }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop loop' })) })
    cleanup()
    renderWith(makeLoop({ active: false }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })

    expect(calls).toEqual([
      '/api/autonudge/l1?intent=stop',
      '/api/autonudge/l1?intent=clear',
    ])
  })

  it('sits on the schedule line, not in the Stop/Save action row (max-two-buttons-per-row)', async () => {
    // `website/AUTOSDE.yaml:230` holds a row to two controls and names this
    // escape itself: the third action "leaves the row". Asserted structurally
    // rather than by counting the whole popover, because the rule is about
    // SIBLINGS IN ONE horizontal group.
    renderWith(makeLoop())
    const save = screen.getByRole('button', { name: 'Save' })
    const row = save.parentElement!
    const rowButtons = Array.from(row.querySelectorAll('button'))
    expect(rowButtons).toHaveLength(2)
    expect(rowButtons.map(b => b.textContent)).toEqual(['Stop loop', 'Save'])
    // And the trigger is a sibling of the schedule text instead.
    const trigger = triggerButton()!
    expect(trigger.parentElement).not.toBe(row)
    expect(trigger.parentElement!.textContent).toMatch(/Last fire:/)
  })
})

describe('AutoNudgePopover {{STOP_FILE}} help line (#10458)', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const helpLine = () => screen.queryByText(/is filled in when each nudge is sent/i)
  const noneLine = () => screen.queryByText(/armed without a stop file/i)

  it('explains the raw token under the default template and names it verbatim', () => {
    renderPopover(null)
    // The stored template is untouched: the server substitutes the token at
    // fire time, so the textarea must still carry it.
    expect(goalBox().value).toContain(STOP_FILE_TOKEN)
    const help = helpLine()
    expect(help, 'no help line rendered under the goal textarea').toBeTruthy()
    // The token is interpolated as text, not left as an i18next placeholder
    // that would have been dropped or rendered as `{{token}}`.
    expect(help!.textContent).toContain(STOP_FILE_TOKEN)
    expect(help!.textContent).not.toContain('{{token}}')
    // Screen readers get the same explanation as sighted readers.
    expect(goalBox().getAttribute('aria-describedby')).toBe(help!.id)
  })

  it('does not render the help line for a goal that carries no token', () => {
    renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'Ship the BYOA gate harness' } })
    expect(helpLine()).toBeNull()
    expect(noneLine()).toBeNull()
    expect(goalBox().hasAttribute('aria-describedby')).toBe(false)
    // Typing the token back brings the line back: it tracks the live text, not the template.
    fireEvent.change(goalBox(), { target: { value: `Do the thing. Halt via ${STOP_FILE_TOKEN}` } })
    expect(helpLine()).toBeTruthy()
  })

  it('Start loop posts the message with the token intact (display never rewrites what is stored)', async () => {
    renderPopover(null)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })
    const calls = (fetch as unknown as { mock: { calls: [string, { body?: string }?][] } }).mock.calls
    const save = calls.find(c => String(c[0]).startsWith('/api/autonudge') && c[1]?.body)
    expect(save, 'no /api/autonudge write was issued').toBeTruthy()
    const body = JSON.parse(save![1]!.body!)
    expect(body.message).toContain(STOP_FILE_TOKEN)
  })

  it('an armed loop with an explicitly empty sentinel says the token goes out blank', () => {
    renderPopover(makeLoop({ message: `Keep going. To halt, create ${STOP_FILE_TOKEN}`, stop_sentinel_path: '' }))
    expect(noneLine()).toBeTruthy()
    expect(noneLine()!.textContent).toContain(STOP_FILE_TOKEN)
    expect(helpLine()).toBeNull()
  })

  it('an armed loop with a sentinel keeps the generic line and never renders the path', () => {
    renderPopover(makeLoop({ message: `Keep going. To halt, create ${STOP_FILE_TOKEN}`, stop_sentinel_path: '/home/someone/.stop-chat-1-100' }))
    expect(helpLine()).toBeTruthy()
    expect(noneLine()).toBeNull()
    expect(screen.queryByText(/\.stop-chat-1-100/)).toBeNull()
  })

  it('a loop record that does not carry the sentinel field (websocket frame) gets the generic line', () => {
    renderPopover(makeLoop({ message: `Keep going. To halt, create ${STOP_FILE_TOKEN}` }))
    expect(helpLine()).toBeTruthy()
    expect(noneLine()).toBeNull()
  })
})

/** Clearing a stopped goal is answered as "Clear goal for good". The press must
 *  therefore write NOTHING to the slot's draft store: copying the erased record
 *  into it would make the freed slot reopen on the goal the user just removed.
 *  The store keeps only what was typed on an empty slot, so a draft from before
 *  the loop is neither resurrected as the record's text nor deleted by the clear.
 *  The real parent (`SessionAutomationPopover`) keys this popover on the loop id,
 *  so the clear is a remount and the close-flush, unmounting with the erased loop
 *  still in hand, skips too -- storage is byte-identical across the whole press. */
describe('AutoNudgePopover confirmed clear writes no draft', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.unstubAllGlobals() })

  /** The DELETE answers `status`; every other read answers empty. A successful
   *  clear is a bodiless 204, so its `json` rejecting pins that the success path
   *  never parses one. */
  function stubDelete(status: number, deletes: string[] = []) {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        deletes.push(String(url))
        return status < 400
          ? Promise.resolve({ ok: true, status, json: () => Promise.reject(new Error('204 carries no body')) })
          : Promise.resolve({ ok: false, status, json: () => Promise.resolve({ error: 'goal changed under you' }) })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).startsWith('/api/crons') ? { jobs: [] } : { loop: null }),
      })
    }) as unknown as typeof fetch)
  }

  /** The real parent, reduced to the three things that matter here: `loop` is
   *  fed back from `onChange`, `open` from `onOpenChange`, and the popover is
   *  KEYED on the loop id exactly as `SessionAutomationPopover` keys it -- that
   *  key is what turns a clear into a remount. A fixed key would let the
   *  close-flush run against a null loop and could write a draft the component
   *  itself never wrote, hiding a reintroduced post-clear write behind it. */
  function renderParent(initial: AutoNudgeLoop, onSlotFreed: () => void = () => {}) {
    const onChange = vi.fn((next: AutoNudgeLoop | null) => { if (next === null) onSlotFreed() })
    const onOpenChange = vi.fn()
    const Parent = () => {
      const [loop, setLoop] = useState<AutoNudgeLoop | null>(initial)
      const [open, setOpen] = useState(true)
      return (
        <>
          <button onClick={() => setOpen(true)}>reopen</button>
          <AutoNudgePopover
            key={loop?.id ?? `bounded:${SLOT}`}
            slotKey={SLOT}
            loop={loop}
            open={open}
            onOpenChange={v => { onOpenChange(v); setOpen(v) }}
            onChange={next => { onChange(next); setLoop(next) }}
          />
        </>
      )
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <Parent />
      </QueryClientProvider>,
    )
    return { onChange, onOpenChange }
  }

  const goal = () => screen.getByLabelText('Goal description') as HTMLTextAreaElement
  const numbers = () => screen.getAllByRole('spinbutton') as HTMLInputElement[]

  async function pressClear() {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })
  }

  /** The empty-slot template, read through the component (an empty slot shows
   *  it; an untouched open writes nothing) rather than duplicated as a string. */
  function readTemplate(): string {
    renderPopover(null)
    const template = goal().value
    cleanup()
    expect(loadGoalDraft(SLOT)).toBeNull()
    return template
  }

  it('leaves the draft store byte-identical and reopens on the template, not the cleared goal (the reported contradiction)', async () => {
    stubDelete(204)
    const template = readTemplate()
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')
    let draftWhenSlotFreed: GoalDraft | null | undefined
    const { onOpenChange } = renderParent(
      makeLoop({ active: false, message: 'finish the migration', idle_secs: 120, max_cycles: 5 }),
      () => { draftWhenSlotFreed = loadGoalDraft(SLOT) },
    )
    // Opening on a loop writes nothing: the record is authoritative until it is gone.
    expect(loadGoalDraft(SLOT)).toBeNull()

    await pressClear()

    // Nothing was written BEFORE the slot was handed back (the window a
    // post-erase write would use) ...
    expect(draftWhenSlotFreed).toBeNull()
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(screen.queryByLabelText('Goal description')).toBeNull()
    // ... nor by the close/remount that followed: byte-identical storage.
    expect(loadGoalDraft(SLOT)).toBeNull()
    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)

    // Reopen on the freed slot: the template, not the goal the user erased.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'reopen' })) })
    expect(goal().value).toBe(template)
    expect(numbers().map(n => n.value)).toEqual(['60', '0'])
    // And it is a fresh start, not a stopped record's controls.
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeTruthy()
    expect(screen.queryByTestId('auto-nudge-loop-paused')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
  })

  it('does not keep an edit made on the stopped record once that record is cleared', async () => {
    // A stopped loop's fields are editable (raising Max cycles is the way back
    // to Start loop). Editing and then confirming the erase is still an erase:
    // the edited text belongs to the record the user just chose to remove, so
    // it must not survive as the slot's draft either.
    stubDelete(204)
    const template = readTemplate()
    renderParent(makeLoop({ active: false, message: 'record text', idle_secs: 90, max_cycles: 3 }))
    fireEvent.change(goal(), { target: { value: 'reworded before clearing' } })
    fireEvent.change(numbers()[1], { target: { value: '8' } })
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')

    await pressClear()

    expect(loadGoalDraft(SLOT)).toBeNull()
    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'reopen' })) })
    expect(goal().value).toBe(template)
    expect(numbers().map(n => n.value)).toEqual(['60', '0'])
  })

  it('neither resurrects the record over, nor deletes, a draft typed before the loop existed', async () => {
    // The draft store holds what the user typed on the EMPTY slot. A record
    // armed afterwards never overwrote it (drafts are not written while a loop
    // is present), and clearing that record must not overwrite it now: the
    // confirmation named the record, not the user's own unsent text. The clear
    // therefore leaves the older draft exactly as it was, and the reopen seeds
    // from it rather than from the erased record.
    stubDelete(204)
    saveGoalDraft(SLOT, { message: 'pre-loop draft', idleSecs: 45, maxCycles: 2 })
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')
    renderParent(makeLoop({ active: false, message: 'record text', idle_secs: 90, max_cycles: 3 }))
    expect(goal().value).toBe('record text')

    await pressClear()

    expect(loadGoalDraft(SLOT)).toEqual({ message: 'pre-loop draft', idleSecs: 45, maxCycles: 2 })
    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'reopen' })) })
    expect(goal().value).toBe('pre-loop draft')
    expect(numbers().map(n => n.value)).toEqual(['45', '2'])
  })

  it('leaves the record, the popover and any prior draft untouched when the server refuses the clear', async () => {
    // A refused erase (409: the record changed under the popover) must not be
    // reported as one. No draft write, no false deletion of the prior draft, no
    // close, no `onChange(null)` -- the fields stay where they were, with the
    // refusal inline, so the user can decide again against what is really there.
    const deletes: string[] = []
    stubDelete(409, deletes)
    saveGoalDraft(SLOT, { message: 'prior draft', idleSecs: 45, maxCycles: 2 })
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')
    const { onChange, onOpenChange } = renderParent(
      makeLoop({ active: false, message: 'finish the migration', idle_secs: 120, max_cycles: 5 }),
    )

    await pressClear()

    expect(deletes).toEqual(['/api/autonudge/l1?intent=clear'])
    expect(screen.getByText('goal changed under you')).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
    // Still the stopped record, still its text, still the primed confirmation
    // (unchanged behaviour: the user can retry against the refusal or back out).
    expect(goal().value).toBe('finish the migration')
    expect(numbers().map(n => n.value)).toEqual(['120', '5'])
    expect(screen.getByTestId('auto-nudge-loop-paused')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Clear goal for good' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    // Byte-identical storage: neither a draft write nor a TTL bump happened.
    expect(loadGoalDraft(SLOT)).toEqual({ message: 'prior draft', idleSecs: 45, maxCycles: 2 })
    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)
  })

  it('writes no draft when stopping an ACTIVE loop, whose record survives and stays authoritative', async () => {
    // A stop keeps the record; on reopen the record seeds the fields, and a
    // draft mirrored from it would be the live-config-in-the-draft-store leak
    // the persistence rules exist to prevent. Pinned with a prior draft in
    // place so "no write" is observable as byte-identical storage rather than
    // as a still-empty store.
    const deletes: string[] = []
    stubDelete(204, deletes)
    saveGoalDraft(SLOT, { message: 'prior draft', idleSecs: 45, maxCycles: 2 })
    const draftsBefore = localStorage.getItem('mc-goal-drafts')
    const tsBefore = localStorage.getItem('mc-goal-drafts-ts')
    const { onChange, onOpenChange } = renderParent(makeLoop({ active: true, message: 'active loop goal' }))

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop loop' })) })

    expect(deletes).toEqual(['/api/autonudge/l1?intent=stop'])
    expect(onChange).toHaveBeenCalledWith(null)
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(localStorage.getItem('mc-goal-drafts')).toBe(draftsBefore)
    expect(localStorage.getItem('mc-goal-drafts-ts')).toBe(tsBefore)
    expect(loadGoalDraft(SLOT)).toEqual({ message: 'prior draft', idleSecs: 45, maxCycles: 2 })
  })
})
