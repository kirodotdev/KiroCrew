import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
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

  it('names the way OUT of a paused loop instead of leaving Save to do it silently', () => {
    // The primary button PATCHes `active: true`, so on a paused loop it is the
    // resume control -- and it used to read "Save", which said nothing. A blind
    // reader found no resume path at all and called "Stop loop" risky as a
    // result. Both directions asserted: an active loop must still read Save, or
    // this would just move the confusion.
    renderWith(makeLoop({ active: false }))
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
    renderWith(makeLoop({ active: false }))
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
