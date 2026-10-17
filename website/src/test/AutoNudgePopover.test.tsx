import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { STOP_FILE_TOKEN, type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import { __resetForTests, loadGoalDraft, saveGoalDraft } from '../utils/goalDrafts'
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
        onFired={() => {}}
      />
    </QueryClientProvider>,
  )
}

const makeLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'active loop goal',
  idle_secs: 90, max_cycles: 3, cycle_count: 1, active: true, last_fire_ts: 0,
  next_due_ts: 0, ...over,
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
        onFired={() => {}}
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
        <AutoNudgePopover slotKey={SLOT} loop={makeLoop({ next_due_ts: deadline })} open={false} onOpenChange={() => {}} onChange={() => {}} onFired={() => {}} />
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
    const props = { slotKey: SLOT, open: true, onOpenChange: () => {}, onChange: () => {}, onFired: () => {} }
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
        onFired={() => {}}
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

  it('shows the capped readout in the popover title, not only on the chip', () => {
    renderPopover(makeLoop({ cycle_count: 3, max_cycles: 24 }))
    expect(screen.getByTestId('auto-nudge-title').textContent).toBe('Goal active (cycle 3/24)')
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

/** The controls while a loop exists are exactly two icon-only buttons, Pause
 *  and Play, in every state; Clear is a text action on a paused loop behind
 *  its confirm; the loop's state is one line by the title (product owner,
 *  2026-10-01). */
describe('AutoNudgePopover status line and Pause | Play controls', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  type Call = [string, { method?: string, body?: string }?]
  const calls = () => (fetch as unknown as { mock: { calls: Call[] } }).mock.calls
  const patchCalls = () => calls().filter(c => c[1]?.method === 'PATCH')
  const createCalls = () => calls().filter(c => String(c[0]) === '/api/autonudge' && c[1]?.method === 'POST')
  const fireCalls = (id = 'l1') => calls().filter(c => String(c[0]) === `/api/autonudge/${id}/fire`)
  const deleteCalls = () => calls().filter(c => c[1]?.method === 'DELETE').map(c => String(c[0]))
  const byLabel = (name: string) => screen.queryByRole('button', { name })
  const status = () => screen.queryByTestId('auto-nudge-status')
  const clearAction = () => screen.queryByTestId('auto-nudge-clear')
  const dirtyDot = () => screen.queryByTestId('auto-nudge-play-dirty')
  const goalBox = () => screen.getByRole('textbox', { name: 'Goal description' }) as HTMLTextAreaElement
  const cyclesField = () => screen.getAllByRole('spinbutton')[1] as HTMLInputElement
  /** Every button in the action row by accessible name: the icon controls carry
   *  theirs in `aria-label`, the confirm's two text buttons as text. */
  const rowNames = () =>
    Array.from(screen.getByTestId('auto-nudge-controls').querySelectorAll('button'))
      .map(b => b.getAttribute('aria-label') ?? b.textContent)
  const title = () => screen.getByTestId('auto-nudge-title').textContent

  const PAUSE = 'Pause loop'
  const NUDGE_NOW = 'Nudge now'
  const SAVE_AND_NUDGE = 'Save edits and nudge now'
  const RESUME = 'Resume loop and nudge now'
  const SAVE_AND_RESUME = 'Save edits, resume loop and nudge now'
  const START = 'Start loop'
  const FRESH_BUDGET = 'Play resumes it with a fresh budget.'

  const running = (over: Partial<AutoNudgeLoop> = {}) => makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 300, ...over })
  const paused = (over: Partial<AutoNudgeLoop> = {}) => makeLoop({ active: false, stopped_reason: 'manual', next_due_ts: 0, ...over })

  /** Controlled `open`, as the real parent wires it, so "stays open" is a
   *  statement about the component and not about a fixed prop. */
  const renderWith = (loop: AutoNudgeLoop | null, onChange = vi.fn(), writeDisabled = false) => {
    const onOpenChange = vi.fn()
    const onFired = vi.fn()
    const Harness = () => {
      const [open, setOpen] = useState(true)
      return (
        <AutoNudgePopover
          slotKey={SLOT}
          loop={loop}
          open={open}
          onOpenChange={v => { onOpenChange(v); setOpen(v) }}
          onChange={onChange}
          onFired={onFired}
          writeDisabled={writeDisabled}
        />
      )
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    )
    return { onChange, onOpenChange, onFired, qc }
  }

  type Refusal = { status: number, error: string }
  type FireAnswer = { ok: true, loop: AutoNudgeLoop } | { ok: false, status: number, error: string }
  /** The WRITE (PATCH on `/api/autonudge/l1`, or the POST create) answers
   *  `written` or a refusal; the fire route answers `fire`; the crons read stays inert. */
  function stubWriteThenFire(written: AutoNudgeLoop | Refusal, fire: FireAnswer) {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      const isWrite = (init?.method === 'PATCH' && String(url) === '/api/autonudge/l1')
        || (init?.method === 'POST' && String(url) === '/api/autonudge')
      if (isWrite) {
        return 'status' in written
          ? Promise.resolve({ ok: false, status: written.status, json: () => Promise.resolve({ error: written.error }) })
          : Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, loop: written }) })
      }
      if (/\/api\/autonudge\/[^/]+\/fire$/.test(String(url))) {
        return fire.ok
          ? Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, loop: fire.loop }) })
          : Promise.resolve({ ok: false, status: fire.status, json: () => Promise.resolve({ error: fire.error }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
    }) as unknown as typeof fetch)
  }

  /** Both row buttons are icon-only: the name in `aria-label`, echoed as the
   *  hover `title`, no visible text, one glyph. */
  function expectIconRow(expected: [string, string]) {
    expect(rowNames()).toEqual(expected)
    for (const name of expected) {
      const button = byLabel(name)!
      expect(button.title, `${name} has no hover text`).toBe(name)
      expect(button.textContent, `${name} renders visible text`).toBe('')
      expect(button.querySelector('svg')).toBeTruthy()
    }
    expect(screen.queryByRole('button', { name: /^(save|stop loop)$/i })).toBeNull()
  }

  it.each([
    ['manual', 'Paused · you paused it'],
    ['autonudge_stop', 'Paused · the agent stopped it'],
    ['cycle_cap', `Paused · cycle limit reached (3 of 3). ${FRESH_BUDGET}`],
    ['runtime_budget', `Paused · time limit reached. ${FRESH_BUDGET}`],
    ['approval_stalled', 'Paused · waiting for your approval'],
    ['structural_terminal', 'Paused · the last cycle could not run'],
    ['session_start_failures', 'Paused · the last cycle could not run'],
    ['some_future_reason', 'Paused'],
    [undefined, 'Paused'],
  ])('a paused loop reads one plain line by the title for stopped_reason %s', (reason, expected) => {
    renderWith(paused({ stopped_reason: reason, cycle_count: 3, max_cycles: 3 }))
    expect(status()!.textContent).toBe(expected)
    expect(status()!.className).toMatch(/\bbg-warn-subtle\b/)
    expect(title()).toBe('Paused')
    expect(screen.queryByText(/Stopped/)).toBeNull()
  })

  it('an active loop held for approval reads paused: warn box, Play resumes, Pause still stops it, no Clear', () => {
    renderWith(running({ approval_stalled: true, next_due_ts: Math.floor(Date.now() / 1000) - 5 }))
    expect(status()!.textContent).toBe('Paused · an approval in this chat timed out. Press Play or send a message to resume.')
    expect(status()!.className).toMatch(/\bbg-warn-subtle\b/)
    expect(title()).toBe('Paused')
    expectIconRow([PAUSE, RESUME])
    // Pause is the one way to stop a held loop from the popover.
    expect(byLabel(PAUSE)!).toBeEnabled()
    // Overdue but nothing armed: the press is how a person resumes it.
    expect(byLabel(RESUME)!).toBeEnabled()
    expect(byLabel(RESUME)!.querySelector('svg.lucide-play')).toBeTruthy()
    // A held loop is not stopped, so the stopped-goal Clear is not offered.
    expect(clearAction()).toBeNull()
  })

  it('an active work-ledger watch waiting on a person says so, and Nudge now still fires it', () => {
    renderWith(running({ waiting_on_person: true }))
    expect(status()!.textContent).toBe('Waiting on you · every open task needs your answer in this chat. Nudges resume when a worker reports back.')
    // The next move is the user's, so it reads in the warn tone, not the running green.
    expect(status()!.className).toMatch(/\bbg-warn-subtle\b/)
    expect(title()).toBe('Goal active (cycle 1/3)')
    expect(clearAction()).toBeNull()
  })

  it('a running loop reads its countdown in the ok box and its cycle in the title; no loop has neither', () => {
    renderWith(running({ cycle_count: 2 }))
    expect(status()!.textContent).toMatch(/^Next cycle in .+$/)
    expect(status()!.className).toMatch(/\bbg-ok-subtle\b/)
    expect(title()).toBe('Goal active (cycle 2/3)')
    cleanup()
    renderWith(null)
    expect(status()).toBeNull()
    expect(title()).toBe('Set a goal')
  })

  const finished = (over: Partial<AutoNudgeLoop> = {}) => makeLoop({ active: false, next_due_ts: 0, cycle_count: 3, ...over })
  const CLEAR_FINISHED = 'Clear finished goal'

  it.each([
    ['the stop file', finished({ stopped_reason: 'stop_sentinel' }), 'Done · the agent created the stop file', true],
    ['a merged pull request', finished({ stopped_reason: 'monitor_terminal', monitor_outcome: 'success', monitor_kind: 'gh-pr' }), 'Done · the watched pull request merged', true],
    ['a pull request closed without merging', finished({ stopped_reason: 'monitor_terminal', monitor_outcome: 'blocked', monitor_kind: 'gh-pr' }), 'Done · the watched pull request was closed without merging', false],
    ['an accepted work ledger (not a pull request)', finished({ stopped_reason: 'monitor_terminal', monitor_outcome: 'success', monitor_kind: 'work-ledger' }), 'Done · the watched subject finished', true],
    ['a rejected work ledger (not a pull request)', finished({ stopped_reason: 'monitor_terminal', monitor_outcome: 'blocked', monitor_kind: 'work-ledger' }), 'Done · the watched subject ended without finishing', false],
  ])('DONE by %s: the title says Done, the info box words the finish (check mark only on a reached goal), the fields are read-only and dimmed with their values kept, and the row is Clear finished goal plus a dead Pause and Play', (_how, loop, expected, check) => {
    renderWith(loop)
    expect(title()).toBe('Done')
    expect(status()!.textContent).toBe(expected)
    expect(status()!.className).toMatch(/\bbg-info-subtle\b/)
    // The hue itself, as every other info wash in the app: `--info-fg` is black
    // in the dark themes, which over a 12% wash is unreadable.
    expect(status()!.className).toMatch(/\btext-info\b/)
    expect(status()!.className).not.toMatch(/bg-warn-subtle|bg-ok-subtle|text-info-fg/)
    const mark = screen.queryByTestId('auto-nudge-done-check')
    expect(mark !== null, 'check mark presence').toBe(check)
    if (mark) expect(mark.tagName.toLowerCase()).toBe('svg')
    expect(screen.queryByText(/Paused/)).toBeNull()
    // Read-only, dimmed, values kept: Done is a dead end, so nothing could be saved.
    for (const field of [goalBox(), ...screen.getAllByRole('spinbutton')]) {
      expect(field).toBeDisabled()
      expect(field.className).toMatch(/\bopacity-60\b/)
    }
    expect(goalBox().value).toBe('active loop goal')
    expect((screen.getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('90')
    expect(cyclesField().value).toBe('3')
    // The row keeps its shape: the link at the left, the two Paused-state glyphs
    // at the right, both dead -- a live Play would promise a resume the service
    // refuses.
    expectIconRow([PAUSE, RESUME])
    expect(byLabel(PAUSE)!).toBeDisabled()
    expect(byLabel(RESUME)!).toBeDisabled()
    expect(byLabel(RESUME)!.querySelector('svg.lucide-play')).toBeTruthy()
    expect(byLabel(RESUME)!.querySelector('svg.lucide-zap')).toBeNull()
    expect(dirtyDot()).toBeNull()
    expect(clearAction()!.textContent).toBe(CLEAR_FINISHED)
    expect(screen.getByTestId('auto-nudge-actions').firstElementChild).toBe(clearAction())
  })

  it('a finished loop whose Play is pressed anyway sends nothing (the control is dead, not merely styled)', async () => {
    renderWith(finished({ stopped_reason: 'stop_sentinel' }))
    await act(async () => { fireEvent.click(byLabel(RESUME)!) })
    await act(async () => { fireEvent.click(byLabel(PAUSE)!) })
    expect(patchCalls()).toHaveLength(0)
    expect(fireCalls()).toHaveLength(0)
  })

  it('Clear finished goal opens the same confirm as Clear stopped goal, with the Done line kept above it, and clears with the clear intent', async () => {
    const { onChange, onOpenChange } = renderWith(finished({ stopped_reason: 'monitor_terminal', monitor_outcome: 'success', monitor_kind: 'gh-pr' }))
    fireEvent.click(clearAction()!)
    expect(rowNames()).toEqual(['Clear', 'Cancel'])
    expect(screen.getByTestId('auto-nudge-clear-question')).toHaveTextContent('Remove this goal for good?')
    expect(status()!.textContent).toBe('Done · the watched pull request merged')
    fireEvent.click(byLabel('Cancel')!)
    expect(clearAction()!.textContent).toBe(CLEAR_FINISHED)
    fireEvent.click(clearAction()!)
    await act(async () => { fireEvent.click(byLabel('Clear')!) })
    expect(deleteCalls()).toEqual(['/api/autonudge/l1?intent=clear'])
    expect(onChange).toHaveBeenCalledWith(null)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('a finished code on a loop that is still active is not Done: the running state wins', () => {
    renderWith(running({ stopped_reason: 'monitor_terminal', monitor_outcome: 'success', monitor_kind: 'gh-pr' }))
    expect(title()).toBe('Goal active (cycle 1/3)')
    expect(status()!.className).toMatch(/\bbg-ok-subtle\b/)
    expect(goalBox()).toBeEnabled()
  })

  it.each([
    ['RUNNING', running(), NUDGE_NOW, SAVE_AND_NUDGE, true, 'lucide-zap'],
    ['PAUSED', paused(), RESUME, SAVE_AND_RESUME, false, 'lucide-play'],
    ['PAUSED at its cycle cap', paused({ stopped_reason: 'cycle_cap', cycle_count: 3, max_cycles: 3 }), RESUME, SAVE_AND_RESUME, false, 'lucide-play'],
    ['PAUSED on its time limit', paused({ stopped_reason: 'runtime_budget' }), RESUME, SAVE_AND_RESUME, false, 'lucide-play'],
  ])('%s: the row is Pause and the fire control, which is live, names its press and wears the state\'s glyph; an edit marks it', (_state, loop, pristineName, dirtyName, pauseLive, glyph) => {
    renderWith(loop)
    expectIconRow([PAUSE, pristineName])
    expect(byLabel(PAUSE)!).toHaveProperty('disabled', !pauseLive)
    expect(byLabel(pristineName)!).toBeEnabled()
    // Lightning while running (nudge now), Play only when there is something to resume.
    expect(byLabel(pristineName)!.querySelector(`svg.${glyph}`)).toBeTruthy()
    expect(dirtyDot()).toBeNull()
    expect(clearAction() !== null).toBe(!loop.active)
    fireEvent.change(cyclesField(), { target: { value: '9' } })
    expectIconRow([PAUSE, dirtyName])
    expect(byLabel(dirtyName)!).toBeEnabled()
    expect(dirtyDot()).toBeTruthy()
    fireEvent.change(cyclesField(), { target: { value: String(loop.max_cycles) } })
    expect(dirtyDot()).toBeNull()
  })

  it('a running loop whose cycle is already due disables Play on a pristine form only: an edit keeps the one save path live', () => {
    renderWith(running({ next_due_ts: Math.floor(Date.now() / 1000) - 5 }))
    expect(byLabel(NUDGE_NOW)!).toBeDisabled()
    expect(byLabel(PAUSE)!).toBeEnabled()
    fireEvent.change(goalBox(), { target: { value: 'edited while due' } })
    expect(byLabel(SAVE_AND_NUDGE)!).toBeEnabled()
  })

  it('NO LOOP: Play alone, named Start loop, creates and starts the loop from the form, fires nothing, hands the created record up and stays open', async () => {
    const created = makeLoop({ id: 'l9', message: 'brand new goal', next_due_ts: 0, cycle_count: 0 })
    stubWriteThenFire(created, { ok: true, loop: created })
    const { onChange, onOpenChange, onFired } = renderWith(null)
    expect(rowNames()).toEqual([START])
    expect(byLabel(START)!.querySelector('svg.lucide-play')).toBeTruthy()
    fireEvent.change(goalBox(), { target: { value: 'brand new goal' } })
    await act(async () => { fireEvent.click(byLabel(START)!) })
    expect(createCalls()).toHaveLength(1)
    expect(JSON.parse(createCalls()[0][1]!.body!)).toEqual({ slot_key: SLOT, message: 'brand new goal', idle_secs: 60, max_cycles: 0 })
    expect(fireCalls('l9')).toHaveLength(0)
    expect(onChange).toHaveBeenCalledWith(created)
    expect(onFired).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
    cleanup()
    renderWith(null)
    fireEvent.change(goalBox(), { target: { value: '   ' } })
    expect(byLabel(START)!).toBeDisabled()
  })

  it('a refused create shows the refusal inline and hands nothing up', async () => {
    stubWriteThenFire({ status: 409, error: 'slot already has a loop' }, { ok: true, loop: makeLoop() })
    const { onChange } = renderWith(null)
    await act(async () => { fireEvent.click(byLabel(START)!) })
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent('slot already has a loop')
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
  })

  it('Pause sends PATCH active:false and nothing else, hands the paused record up and stays open; a refusal lands inline', async () => {
    const pausedRecord = paused()
    stubWriteThenFire(pausedRecord, { ok: true, loop: pausedRecord })
    const { onChange, onOpenChange } = renderWith(running())
    fireEvent.change(goalBox(), { target: { value: 'edited but not saved' } })
    await act(async () => { fireEvent.click(byLabel(PAUSE)!) })
    expect(patchCalls()).toHaveLength(1)
    expect(JSON.parse(patchCalls()[0][1]!.body!)).toEqual({ active: false })
    expect(onChange).toHaveBeenCalledWith(pausedRecord)
    expect(onOpenChange).not.toHaveBeenCalled()
    expect(goalBox().value).toBe('edited but not saved')
    cleanup()
    stubWriteThenFire({ status: 503, error: 'audit log unavailable' }, { ok: true, loop: pausedRecord })
    const second = renderWith(running())
    await act(async () => { fireEvent.click(byLabel(PAUSE)!) })
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent('audit log unavailable')
    expect(second.onChange).not.toHaveBeenCalled()
  })

  it('Play on a pristine RUNNING loop writes nothing and fires on the loop it was pressed on', async () => {
    const loop = running()
    stubWriteThenFire(loop, { ok: true, loop })
    const { onChange, onFired, onOpenChange } = renderWith(loop)
    await act(async () => { fireEvent.click(byLabel(NUDGE_NOW)!) })
    expect(patchCalls()).toHaveLength(0)
    expect(fireCalls()).toHaveLength(1)
    expect(fireCalls()[0][1]).toEqual({ method: 'POST' })
    expect(onChange).not.toHaveBeenCalled()
    expect(onFired).toHaveBeenCalledWith(loop)
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('Play on an EDITED running loop saves only the edited fields (never active), then fires on the written record; the form then holds what the server stored and a second press writes nothing', async () => {
    const stored = running({ idle_secs: 15, message: 'active loop goal' })
    stubWriteThenFire(stored, { ok: true, loop: stored })
    const { onChange, onFired } = renderWith(running())
    fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '1' } })
    await act(async () => { fireEvent.click(byLabel(SAVE_AND_NUDGE)!) })
    expect(patchCalls()).toHaveLength(1)
    expect(JSON.parse(patchCalls()[0][1]!.body!)).toEqual({ idle_secs: 1 })
    expect(fireCalls()).toHaveLength(1)
    expect(onChange).toHaveBeenCalledWith(stored)
    expect(onFired).toHaveBeenCalledWith(stored)
    expect(onChange.mock.invocationCallOrder[0]).toBeLessThan(onFired.mock.invocationCallOrder[0])
    expect((screen.getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('15')
    expect(byLabel(NUDGE_NOW)).toBeTruthy()
    expect(dirtyDot()).toBeNull()
    await act(async () => { fireEvent.click(byLabel(NUDGE_NOW)!) })
    expect(patchCalls()).toHaveLength(1)
    expect(fireCalls()).toHaveLength(2)
  })

  it('a refused fire after a landed write shows the refusal inline, keeps the write, reports no fire and stays open', async () => {
    const stored = running({ message: 'new goal' })
    stubWriteThenFire(stored, { ok: false, status: 409, error: 'a turn is in flight' })
    const { onChange, onFired, onOpenChange } = renderWith(running())
    fireEvent.change(goalBox(), { target: { value: 'new goal' } })
    await act(async () => { fireEvent.click(byLabel(SAVE_AND_NUDGE)!) })
    expect(onChange).toHaveBeenCalledWith(stored)
    expect(onFired).not.toHaveBeenCalled()
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent('a turn is in flight')
    expect(onOpenChange).not.toHaveBeenCalled()
    expect(goalBox().value).toBe('new goal')
  })

  it.each([
    ['a pristine form', false, { active: true }],
    ['an edited form', true, { max_cycles: 9, active: true }],
  ])('Play on a PAUSED loop with %s resumes with active:true (plus only the edited fields) and then fires -- one request path, no bound to raise first', async (_form, edit, body) => {
    const resumed = running({ max_cycles: edit ? 9 : 3 })
    stubWriteThenFire(resumed, { ok: true, loop: resumed })
    const { onChange, onFired } = renderWith(paused({ stopped_reason: 'cycle_cap', cycle_count: 3, max_cycles: 3 }))
    if (edit) fireEvent.change(cyclesField(), { target: { value: '9' } })
    await act(async () => { fireEvent.click(byLabel(edit ? SAVE_AND_RESUME : RESUME)!) })
    expect(patchCalls()).toHaveLength(1)
    expect(JSON.parse(patchCalls()[0][1]!.body!)).toEqual(body)
    expect(fireCalls()).toHaveLength(1)
    expect(onChange).toHaveBeenCalledWith(resumed)
    expect(onFired).toHaveBeenCalledWith(resumed)
  })

  it('a refused resume fires nothing', async () => {
    stubWriteThenFire({ status: 404, error: 'loop gone' }, { ok: true, loop: running() })
    const { onFired } = renderWith(paused())
    await act(async () => { fireEvent.click(byLabel(RESUME)!) })
    expect(fireCalls()).toHaveLength(0)
    expect(onFired).not.toHaveBeenCalled()
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent('loop gone')
  })

  it('Clear is a text action on a paused loop: it asks first in the row (never on the status line), Cancel backs out, Clear deletes with the clear intent, hands null up and closes', async () => {
    const { onChange, onOpenChange, qc } = renderWith(paused())
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const clear = clearAction()!
    expect(clear.tagName).toBe('BUTTON')
    expect(clear.textContent).toBe('Clear stopped goal')
    expect(clear.className).not.toMatch(/border-border|rounded-md/)
    // One row: the link at the left of the same flex row as the two icons.
    expect(screen.getByTestId('auto-nudge-actions')).toContainElement(clear)
    expect(screen.getByTestId('auto-nudge-actions').firstElementChild).toBe(clear)
    fireEvent.click(clear)
    expect(rowNames()).toEqual(['Clear', 'Cancel'])
    const box = screen.getByTestId('auto-nudge-actions')
    expect(box.className).toMatch(/\bborder-danger\b/)
    expect(box).toContainElement(screen.getByTestId('auto-nudge-clear-question'))
    expect(screen.getByTestId('auto-nudge-clear-question')).toHaveTextContent('Remove this goal for good?')
    expect(screen.getByTestId('auto-nudge-clear-question').className).toMatch(/\bfont-medium\b/)
    expect(screen.getByTestId('auto-nudge-clear-question').className).toMatch(/\btext-text\b/)
    expect(byLabel('Clear')!.className).toMatch(/\bbg-danger\b/)
    expect(byLabel('Cancel')!.className).not.toMatch(/\bbg-danger\b/)
    expect(status()!.textContent).toBe('Paused · you paused it')
    fireEvent.click(byLabel('Cancel')!)
    expectIconRow([PAUSE, RESUME])
    fireEvent.click(clearAction()!)
    await act(async () => { fireEvent.click(byLabel('Clear')!) })
    expect(deleteCalls()).toEqual(['/api/autonudge/l1?intent=clear'])
    expect(invalidate).toHaveBeenCalledWith({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
    expect(onChange).toHaveBeenCalledWith(null)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('a primed confirmation is dropped when the record changes under the popover', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const props = { slotKey: SLOT, open: true, onOpenChange: () => {}, onChange: () => {}, onFired: () => {} }
    const view = render(<QueryClientProvider client={qc}><AutoNudgePopover {...props} loop={paused()} /></QueryClientProvider>)
    fireEvent.click(clearAction()!)
    expect(rowNames()).toEqual(['Clear', 'Cancel'])
    view.rerender(<QueryClientProvider client={qc}><AutoNudgePopover {...props} loop={paused({ message: 'a different goal' })} /></QueryClientProvider>)
    expectIconRow([PAUSE, RESUME])
  })

  it('a refused clear shows the refusal inline, hands nothing up and stays open', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => Promise.resolve(
      init?.method === 'DELETE'
        ? { ok: false, status: 409, json: () => Promise.resolve({ error: "this goal's state changed" }) }
        : { ok: true, json: () => Promise.resolve({ loop: null }) },
    )) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(paused())
    fireEvent.click(clearAction()!)
    await act(async () => { fireEvent.click(byLabel('Clear')!) })
    expect(screen.getByTestId('auto-nudge-error')).toHaveTextContent("this goal's state changed")
    expect(onChange).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('a write answered without the record (a legacy stub) keeps the sent values as the baseline and fires on the loop as held', async () => {
    const loop = running()
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(init?.method === 'PATCH' || /\/fire$/.test(String(url)) ? { ok: true } : { loop: null }),
    })) as unknown as typeof fetch)
    const { onFired } = renderWith(loop)
    fireEvent.change(goalBox(), { target: { value: 'typed goal' } })
    fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '45' } })
    fireEvent.change(cyclesField(), { target: { value: '7' } })
    await act(async () => { fireEvent.click(byLabel(SAVE_AND_NUDGE)!) })
    expect(fireCalls()).toHaveLength(1)
    expect(onFired).toHaveBeenCalledWith(loop)
    expect(goalBox().value).toBe('typed goal')
    expect((screen.getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('45')
    expect(cyclesField().value).toBe('7')
    expect(dirtyDot()).toBeNull()
  })

  it('writes disabled (a crew or member session): Pause and Play are dead on a running and a paused loop, nothing is sent, and Clear -- a delete, not a write -- stays live on the paused one', async () => {
    renderWith(running(), vi.fn(), true)
    expect(byLabel(PAUSE)!).toBeDisabled()
    expect(byLabel(NUDGE_NOW)!).toBeDisabled()
    expect(clearAction()).toBeNull()
    // The countdown keeps its words but loses the ok tone: a green box beside
    // the capability note would assert a fire this session cannot act on.
    expect(status()!.textContent).toMatch(/^Next cycle in .+$/)
    expect(status()!.className).not.toMatch(/\bbg-ok-subtle\b/)
    expect(status()!.className).toMatch(/\btext-muted\b/)
    cleanup()
    renderWith(paused(), vi.fn(), true)
    expect(byLabel(PAUSE)!).toBeDisabled()
    expect(byLabel(RESUME)!).toBeDisabled()
    expect(clearAction()!).toBeEnabled()
    fireEvent.click(clearAction()!)
    expect(byLabel('Clear')!).toBeEnabled()
    expect(calls().filter(c => c[1]?.method)).toHaveLength(0)
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

  it('the default template tells the model to call autonudge_stop, not to create a stop file', () => {
    renderPopover(null)
    expect(goalBox().value).toContain('call the autonudge_stop tool')
    expect(goalBox().value).not.toContain(STOP_FILE_TOKEN)
    expect(helpLine()).toBeNull()
  })

  it('explains the raw token in a typed goal and names it verbatim', () => {
    renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: `Keep going. To halt, create ${STOP_FILE_TOKEN}` } })
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
    fireEvent.change(goalBox(), { target: { value: `Keep going. To halt, create ${STOP_FILE_TOKEN}` } })
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
