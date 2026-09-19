import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { STOP_FILE_TOKEN, type AutoNudgeLoop } from '../components/AutoNudgePopover'
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

  const triggerButton = () => screen.queryByRole('button', { name: 'Save edits and nudge now' })

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

  it('offers it NOWHERE for a stopped loop, because the server refuses to fire one', () => {
    // Gated on `active`, not on `loop`: every terminal bound leaves the loop
    // inactive, so a button here could only ever produce a 409. (A loop the
    // user PAUSED keeps the control, disabled -- see the icon-controls block.)
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

  it('names the way OUT of a stopped loop: one accented Play, and no Save to do it silently', () => {
    // A stopped loop's way back is its own control -- Play, labelled "Start
    // loop and nudge now" -- which saves the form, revives the loop and fires.
    // There is no separate Save on an inactive loop: the one control does it
    // all, in the accent the primary action wears. It used to be one button
    // reading "Save", which said nothing about resuming; a blind reader found
    // no resume path at all and called "Stop loop" risky as a result. Both
    // directions asserted: a running loop has Save and no Play, or this would
    // just move the confusion.
    renderWith(makeLoop({ active: false }))
    expect(screen.getByRole('button', { name: 'Start loop and nudge now' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Start loop and nudge now' }).className).toContain('bg-accent')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Start loop and nudge now' })).toBeNull()
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

  it('saves an EDITED form first, then posts to the loop-scoped fire route with NO body -- so the form as it reads is what fires', async () => {
    const saved = makeLoop({ message: 'edited then triggered' })
    const fired = makeLoop({ message: 'edited then triggered', next_due_ts: 1_700_000_000 })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(
          String(url).endsWith('/fire') ? { ok: true, loop: fired }
            : init?.method === 'PATCH' ? { ok: true, loop: saved }
              : { loop: null },
        ),
      }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'edited then triggered' } })

    await act(async () => { fireEvent.click(triggerButton()!) })

    // Selected by URL, not by index: opening the popover also reads /api/crons.
    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const patch = calls.find(c => String(c[0]) === '/api/autonudge/l1' && c[1]?.method === 'PATCH')
    const fire = calls.find(c => String(c[0]) === '/api/autonudge/l1/fire')
    expect(patch, 'no PATCH of the form was issued').toBeTruthy()
    expect(fire, 'no POST to the fire route was issued').toBeTruthy()
    // Every trigger implicitly saves: the three fields, and NEVER `active` --
    // a running loop's save must not be able to revive a loop another tab
    // paused between render and press.
    expect(JSON.parse(patch![1]!.body!)).toEqual({ message: 'edited then triggered', idle_secs: 90, max_cycles: 3 })
    // The fire itself carries no body: what fires is what the loop now holds,
    // which the PATCH a moment earlier made the form's text.
    expect(fire![1]?.method).toBe('POST')
    expect(fire![1]?.body).toBeUndefined()
    expect(calls.indexOf(patch!)).toBeLessThan(calls.indexOf(fire!))
    // The saved record is handed up, then the due reading: the server no
    // longer moves the deadline, so the component supplies the armed one.
    expect(onChange).toHaveBeenNthCalledWith(1, saved)
    const passed = onChange.mock.calls.at(-1)?.[0]
    expect(passed).toMatchObject({ ...fired, next_due_ts: expect.any(Number) })
    expect(passed.next_due_ts).toBeGreaterThan(Date.now() / 1000 - 5)
    // Stays open, like every control that fires or changes the run state: the
    // outcome (the schedule line reading due) is visible in place, and a
    // refusal needs somewhere to land. Only Save closes.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('writes NOTHING on a pristine form: the armed goal fires as the loop holds it, so a revision that landed while the popover sat open survives', async () => {
    // The fields seed on the open edge and never re-sync. A `monitor_update`
    // from the nudged agent (or another tab's save) that lands while the
    // popover sits open therefore changes the RECORD and not the form -- and a
    // Trigger that always wrote the form would write the stale text straight
    // back over it, then fire that. The rule: no edit, no write.
    const armed = makeLoop({ message: 'armed goal, as opened' })
    const revised = makeLoop({ message: 'revised by monitor_update while open' })
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(String(url).endsWith('/fire') ? { ok: true, loop: revised } : { loop: null }) }),
    ) as unknown as typeof fetch)
    const onChange = vi.fn()
    let revise: (loop: AutoNudgeLoop) => void = () => {}
    const Harness = () => {
      const [loop, setLoop] = useState<AutoNudgeLoop>(armed)
      revise = setLoop
      return <AutoNudgePopover slotKey={SLOT} loop={loop} open={true} onOpenChange={() => {}} onChange={onChange} />
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(<QueryClientProvider client={qc}><Harness /></QueryClientProvider>)
    // The revision arrives over the websocket: the record moves, the form does not.
    act(() => revise(revised))
    expect((screen.getByLabelText('Goal description') as HTMLTextAreaElement).value).toBe('armed goal, as opened')

    await act(async () => { fireEvent.click(triggerButton()!) })

    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    expect(calls.filter(c => c[1]?.method === 'PATCH'), 'a pristine form was written back').toEqual([])
    const fire = calls.find(c => String(c[0]) === '/api/autonudge/l1/fire')
    expect(fire, 'no POST to the fire route was issued').toBeTruthy()
    expect(fire![1]?.body).toBeUndefined()
    // Only the due reading is handed up -- nothing was written -- and it
    // carries the revision, because that is what the loop holds and fired.
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange.mock.calls[0][0]).toMatchObject({ message: 'revised by monitor_update while open' })
  })

  it('a second press after a saved edit writes nothing again: the saved fields are the new pristine baseline', async () => {
    // Otherwise every press after the first would re-send the same fields,
    // and the second press is exactly the one that can land after a revision.
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(
          String(url).endsWith('/fire') || init?.method === 'PATCH' ? { ok: true, loop: makeLoop({ message: 'edited once' }) } : { loop: null },
        ),
      }),
    ) as unknown as typeof fetch)
    renderWith(makeLoop())
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'edited once' } })
    const patches = () => (fetch as unknown as { mock: { calls: [string, { method?: string }?][] } }).mock.calls.filter(c => c[1]?.method === 'PATCH')

    await act(async () => { fireEvent.click(triggerButton()!) })
    expect(patches()).toHaveLength(1)
    await act(async () => { fireEvent.click(triggerButton()!) })
    expect(patches()).toHaveLength(1)
  })

  it('keeps the typed goal in the textarea after a successful press (it is now saved, not lost)', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(
          String(url).endsWith('/fire') || init?.method === 'PATCH' ? { ok: true, loop: makeLoop({ message: 'edited and saved' }) } : { loop: null },
        ),
      }),
    ) as unknown as typeof fetch)
    renderWith(makeLoop())
    const box = screen.getByLabelText('Goal description') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'edited and saved' } })

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect((screen.getByLabelText('Goal description') as HTMLTextAreaElement).value)
      .toBe('edited and saved')
  })

  it('surfaces a refused fire inline and keeps the popover open; the save that preceded it stands', async () => {
    // The refusal names the outcome and the next step, not just the condition:
    // a reader must be able to tell a refusal from a delay, and the press was
    // refused rather than queued.
    const REFUSAL = 'nudge not sent: the agent is still working, so try again when it finishes'
    const saved = makeLoop({ message: 'edited, then refused' })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      String(url).endsWith('/fire')
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: REFUSAL, code: 'session_busy' }) })
        : Promise.resolve({ ok: true, json: () => Promise.resolve(init?.method === 'PATCH' ? { ok: true, loop: saved } : { loop: null }) }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())
    // An edit, so there IS a save to precede the fire (a pristine form writes nothing).
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'edited, then refused' } })

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect(screen.getByText(REFUSAL)).toBeTruthy()
    // The save landed and is handed up once; the fire's refusal does not undo it.
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(saved)
    // A refusal must not report success by tearing the popover down.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('names the object it clears once the loop is already stopped, and says the erase is final', async () => {
    // One button, two actions. On a live loop the press stops the loop and keeps
    // the record. On a stopped one there is nothing left to stop: the press
    // removes it, which is the only way the slot can watch something else -- a
    // stopped structured monitor blocks a re-arm until its row is gone.
    // "Clear record" failed a blind read (the popover shows nothing called a
    // "record"), so the label names the GOAL and the status reads Stopped rather
    // than the resumable-sounding Paused; nothing explanatory renders under it
    // (product owner, 2026-09-17). Both directions asserted so this cannot
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
    // The status word and the controls, and nothing explanatory under them
    // (product owner, 2026-09-17): no helper sentence, and the erase question
    // renders only once the confirm is up.
    expect(screen.queryByTestId('auto-nudge-clear-question')).toBeNull()
    expect(screen.queryByText(/removes it for good/)).toBeNull()
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Stop loop' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
    expect(screen.queryByTestId('auto-nudge-clear-question')).toBeNull()
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
    // And the question renders on the schedule line while the confirm is up:
    // the confirm row itself asks nothing.
    expect(screen.getByTestId('auto-nudge-clear-question').textContent)
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

  it('sits in the action row, in the right cluster beside Pause and Save, with Stop alone on the left (operator ruling 2026-09-17)', async () => {
    // ONE lane for every control. This knowingly exceeds
    // `website/AUTOSDE.yaml:230` (`max-two-buttons-per-row`): the product owner
    // ruled the layout -- Stop isolated left as the destructive control, the
    // loop's transport and save controls clustered right, an overflow menu
    // rejected because every control must stay visible. Asserted structurally
    // and by ORDER, read by aria-label: the controls are icon buttons, so their
    // text content is empty by design.
    renderWith(makeLoop())
    const row = screen.getByTestId('auto-nudge-actions')
    expect(Array.from(row.querySelectorAll('button')).map(b => b.getAttribute('aria-label')))
      .toEqual(['Stop loop', 'Save edits and nudge now', 'Pause loop', 'Save'])
    // Stop is the row's own child; the other three share the right-hand cluster.
    expect(screen.getByRole('button', { name: 'Stop loop' }).parentElement).toBe(row)
    const cluster = screen.getByTestId('auto-nudge-loop-controls')
    expect(triggerButton()!.parentElement).toBe(cluster)
    expect(Array.from(cluster.querySelectorAll('button')).map(b => b.getAttribute('aria-label')))
      .toEqual(['Save edits and nudge now', 'Pause loop', 'Save'])
    // And the schedule line is text only -- the button left it.
    expect(screen.getByTestId('auto-nudge-schedule').querySelectorAll('button')).toHaveLength(0)
    expect(screen.getByTestId('auto-nudge-schedule').textContent).toMatch(/Last fire:/)
  })
})

/** ONE lane for every control (operator ruling, 2026-09-17): Stop pinned left
 *  as the destructive control, the loop's transport and save controls clustered
 *  right. RUNNING: [Stop] .. [Trigger][Pause][Save]. PAUSED -- `stopped_reason:
 *  'manual'`, which is what a `PATCH active:false` records, and the only reason
 *  that reads "Paused": [Stop] .. [Play], one accented control that saves the
 *  form, resumes and fires. STOPPED by a bound or a tool: the same shape, Stop
 *  being the two-step erase and Play reading "Start loop and nudge now". NO
 *  LOOP: one accented Play that creates the loop from the form and fires it.
 *  Every fire on this surface persists the form first ("any Trigger implicitly
 *  calls the Save logic"); Save alone exists only on a running loop. */
describe('AutoNudgePopover one-lane icon controls (Stop | Trigger Pause Save)', () => {
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
  /** The action row's buttons in DOM order, by aria-label: icon buttons carry no text. */
  const rowLabels = () =>
    Array.from(screen.getByTestId('auto-nudge-actions').querySelectorAll('button')).map(b => b.getAttribute('aria-label'))
  const clusterLabels = () =>
    Array.from(screen.getByTestId('auto-nudge-loop-controls').querySelectorAll('button')).map(b => b.getAttribute('aria-label'))
  const PLAY_RESUME = 'Resume loop and nudge now'
  const PLAY_START = 'Start loop and nudge now'
  const TRIGGER = 'Save edits and nudge now'

  /** Controlled `open`, as the real parent wires it, so "stays open" is a
   *  statement about the component and not about a fixed prop. */
  const renderWith = (loop: AutoNudgeLoop | null, onChange = vi.fn(), writeDisabled = false) => {
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
    return { onChange, onOpenChange }
  }

  /** Answer the PATCH with the record the server would return, so the
   *  component's `onChange` hand-off can be asserted on real data. */
  function stubPatch(returned: AutoNudgeLoop) {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(
          init?.method === 'PATCH' && String(url) === '/api/autonudge/l1' ? { ok: true, loop: returned } : { loop: null },
        ),
      }),
    ) as unknown as typeof fetch)
  }

  type Refusal = { status: number, error: string }
  type FireAnswer = { ok: true, loop: AutoNudgeLoop } | { ok: false, status: number, error: string }
  /** Both legs of a Play or Trigger press: the WRITE (PATCH on `/api/autonudge/l1`,
   *  or the POST create on `/api/autonudge`) answers `written` (or a refusal),
   *  the fire route answers `fire`. Everything else (the crons read) stays inert. */
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

  const paused = (over: Partial<AutoNudgeLoop> = {}) =>
    makeLoop({ active: false, stopped_reason: 'manual', next_due_ts: 0, ...over })
  const stoppedBy = (reason: string) => makeLoop({ active: false, next_due_ts: 0, stopped_reason: reason })
  const running = () => makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 300 })

  /** Every button in the action row is an icon button: aria-label, a matching
   *  title (the hover tooltip repeats the label), no text, a glyph. */
  function expectIconRow(expected: string[]) {
    expect(rowLabels()).toEqual(expected)
    for (const name of expected) {
      const button = byLabel(name)!
      expect(button, `${name} is missing`).toBeTruthy()
      expect(button.getAttribute('title')).toBe(name)
      expect(button.textContent).toBe('')
      expect(button.querySelector('svg')).toBeTruthy()
    }
  }

  it('RUNNING: one row -- Stop alone on the left, Trigger, Pause and Save clustered right, in that order', () => {
    renderWith(running())
    expectIconRow(['Stop loop', TRIGGER, 'Pause loop', 'Save'])
    // Stop is the row's own child; the three others share the right-hand
    // cluster, so the free space falls between Stop and the cluster.
    const row = screen.getByTestId('auto-nudge-actions')
    expect(byLabel('Stop loop')!.parentElement).toBe(row)
    expect(clusterLabels()).toEqual([TRIGGER, 'Pause loop', 'Save'])
    // Danger and primary read at rest, not on hover: a touch viewport never
    // hovers, and the glyphs alone do not say "removes" or "primary".
    expect(byLabel('Stop loop')!.className).toContain('text-danger')
    expect(byLabel('Save')!.className).toContain('bg-accent')
    // The schedule line is text only: the transport controls left it.
    expect(screen.getByTestId('auto-nudge-schedule').querySelectorAll('button')).toHaveLength(0)
    expect((byLabel(TRIGGER) as HTMLButtonElement).disabled).toBe(false)
    // Trigger saves the form, so an empty goal disables it exactly like Save.
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: '  ' } })
    expect((byLabel(TRIGGER) as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel('Save') as HTMLButtonElement).disabled).toBe(true)
    cleanup()
    // Already due: the press would do nothing, so Trigger is disabled.
    renderWith(makeLoop({ next_due_ts: 1_700_000_000 }))
    expect((byLabel(TRIGGER) as HTMLButtonElement).disabled).toBe(true)
  })

  it('Pause sends PATCH active:false and nothing else, keeps the popover open, and hands the paused record up', async () => {
    const pausedRecord = paused()
    stubPatch(pausedRecord)
    const { onChange, onOpenChange } = renderWith(running())

    await act(async () => { fireEvent.click(byLabel('Pause loop')!) })

    expect(patchCalls()).toHaveLength(1)
    const [url, init] = patchCalls()[0]
    expect(url).toBe('/api/autonudge/l1')
    // ONLY `active`: a pause must not also persist whatever sits in the
    // fields, and it must never fire anything.
    expect(JSON.parse(init!.body!)).toEqual({ active: false })
    expect(fireCalls()).toHaveLength(0)
    expect(onChange).toHaveBeenCalledWith(pausedRecord)
    // Like Trigger, and unlike Stop/Save: closing would drop an unsaved edit
    // in the textarea, and the state change is visible in place.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('PAUSED: [Stop] .. [Play] -- one accented control, no Save, no Trigger, and the status reads Paused', () => {
    renderWith(paused())
    expectIconRow(['Stop loop', PLAY_RESUME])
    expect(clusterLabels()).toEqual([PLAY_RESUME])
    // Play IS the primary here, in the accent Save wears on a running loop.
    expect(byLabel(PLAY_RESUME)!.className).toContain('bg-accent')
    expect(byLabel('Save')).toBeNull()
    expect(byLabel('Pause loop')).toBeNull()
    // Complement assertion, not a bare negative on one node: a stale render
    // could leave the trigger somewhere else in the tree.
    expect(screen.queryAllByRole('button', { name: /nudge now/i })).toHaveLength(1)
    expect(screen.getByTestId('auto-nudge-loop-paused-manually').textContent).toBe('Paused')
    // The status word and the controls, nothing explanatory (product owner,
    // 2026-09-17): no helper sentence, no question until the confirm is up.
    expect(screen.queryByTestId('auto-nudge-clear-question')).toBeNull()
    expect(screen.queryByText(/removes it for good|saves your edits/)).toBeNull()
    // NOT the stopped path: no "Stopped", no erase-with-confirm, no Start loop.
    expect(screen.queryByTestId('auto-nudge-loop-paused')).toBeNull()
    expect(byLabel('Clear stopped goal')).toBeNull()
    expect(byLabel(PLAY_START)).toBeNull()
  })

  it('Play on a PRISTINE paused loop is resume + run-now: PATCH {active:true} alone -- no field written back -- then POST fire, popover left open', async () => {
    const resumed = makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 90 })
    stubWriteThenFire(resumed, { ok: true, loop: resumed })
    const { onChange, onOpenChange } = renderWith(paused())

    await act(async () => { fireEvent.click(byLabel(PLAY_RESUME)!) })

    // Leg 1: the loop goes back to work. Nothing was edited, so the PATCH
    // carries `active` and NOTHING else: the fields seed on open and never
    // re-sync, and writing them back here would revert a revision that landed
    // while the loop sat paused -- what resumes is what the record holds.
    expect(patchCalls()).toHaveLength(1)
    expect(JSON.parse(patchCalls()[0][1]!.body!)).toEqual({ active: true })
    // Leg 2: the fire, with NO body, AFTER the PATCH -- `fire_now` refuses an
    // inactive loop with 409, so the order is load-bearing.
    expect(fireCalls()).toHaveLength(1)
    expect(fireCalls()[0][1]?.method).toBe('POST')
    expect(fireCalls()[0][1]?.body).toBeUndefined()
    const order = calls().map(c => `${c[1]?.method ?? 'GET'} ${c[0]}`)
    expect(order.indexOf('PATCH /api/autonudge/l1')).toBeLessThan(order.indexOf('POST /api/autonudge/l1/fire'))
    // The resumed record is handed up first, then the due reading the fire arms.
    expect(onChange).toHaveBeenNthCalledWith(1, resumed)
    expect(onChange).toHaveBeenCalledTimes(2)
    const due = onChange.mock.calls[1][0] as AutoNudgeLoop
    expect(due).toMatchObject({ id: 'l1', active: true })
    expect(Math.abs(due.next_due_ts - Date.now() / 1000)).toBeLessThan(5)
    // Stays open: the outcome is visible in place (Pause is back, the schedule
    // line reads due), and a fire refusal needs somewhere to land.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it.each([
    ['paused', () => paused(), PLAY_RESUME],
    ['stopped', () => stoppedBy('cycle_cap'), PLAY_START],
  ])('Play on an EDITED %s loop sends the form as it reads with active:true: an edited goal, interval and cap ride the resume, then the fire', async (_state, loop, play) => {
    // The other half of the rule: an edit IS saved by Play -- pause, edit the
    // goal, interval or cap, press Play, no separate Save -- and a cap raised
    // in the form travels with the revive instead of the loop re-stopping a
    // tick later on the spent cap.
    const resumed = makeLoop({ message: 'edited before play', idle_secs: 120, max_cycles: 50 })
    stubWriteThenFire(resumed, { ok: true, loop: resumed })
    renderWith(loop())
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'edited before play' } })
    fireEvent.change(screen.getByLabelText('Seconds between nudges'), { target: { value: '120' } })
    fireEvent.change(screen.getByLabelText('Max cycles (0 = infinite)'), { target: { value: '50' } })

    await act(async () => { fireEvent.click(byLabel(play)!) })

    expect(patchCalls()).toHaveLength(1)
    expect(JSON.parse(patchCalls()[0][1]!.body!)).toEqual({ message: 'edited before play', idle_secs: 120, max_cycles: 50, active: true })
    expect(fireCalls()).toHaveLength(1)
    const order = calls().map(c => `${c[1]?.method ?? 'GET'} ${c[0]}`)
    expect(order.indexOf('PATCH /api/autonudge/l1')).toBeLessThan(order.indexOf('POST /api/autonudge/l1/fire'))
  })

  it('a 409 on the fire leg leaves the loop RESUMED and shows the refusal inline; nothing is rolled back', async () => {
    const REFUSAL = 'loop is already firing'
    const resumed = makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 90 })
    stubWriteThenFire(resumed, { ok: false, status: 409, error: REFUSAL })
    const { onChange, onOpenChange } = renderWith(paused())

    await act(async () => { fireEvent.click(byLabel(PLAY_RESUME)!) })

    expect(patchCalls()).toHaveLength(1)
    expect(fireCalls()).toHaveLength(1)
    // The resume stands: the record handed up is the resumed one, once.
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(resumed)
    expect(screen.getByText(REFUSAL)).toBeTruthy()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('a refused resume fires nothing: no POST follows a failed PATCH', async () => {
    const REFUSAL = 'audit log unavailable — nudge loop not updated'
    stubWriteThenFire({ status: 503, error: REFUSAL }, { ok: true, loop: makeLoop() })
    const { onChange } = renderWith(paused())

    await act(async () => { fireEvent.click(byLabel(PLAY_RESUME)!) })

    expect(patchCalls()).toHaveLength(1)
    expect(fireCalls()).toHaveLength(0)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByText(REFUSAL)).toBeTruthy()
  })

  it.each([
    ['cycle_cap', 'a spent cycle cap'],
    ['runtime_budget', 'a spent wall-clock budget'],
    ['approval_stalled', 'an approval stall'],
    ['autonudge_stop', 'the autonudge_stop tombstone'],
    ['', 'a stop with no recorded reason'],
  ])('STOPPED by %s keeps the same shape -- Stop is the two-step erase, Play reads Start loop, no Save -- and still says Stopped, never Paused', async (reason) => {
    renderWith(stoppedBy(reason))
    expect(screen.getByTestId('auto-nudge-loop-paused').textContent).toBe('Stopped')
    expect(screen.queryByTestId('auto-nudge-clear-question')).toBeNull()
    expectIconRow(['Clear stopped goal', PLAY_START])
    expect(clusterLabels()).toEqual([PLAY_START])
    expect(byLabel(PLAY_START)!.className).toContain('bg-accent')
    // None of the running/paused-only controls leak into a stopped loop, and
    // the status is not the resumable one.
    for (const name of ['Pause loop', PLAY_RESUME, TRIGGER, 'Stop loop', 'Save']) {
      expect(byLabel(name), `${name} rendered on a stopped loop`).toBeNull()
    }
    expect(screen.queryByTestId('auto-nudge-loop-paused-manually')).toBeNull()

    // Stop here is the erase of the retained record, and it asks first: the
    // row becomes the confirm (Cancel / Clear goal for good), the question
    // appears on the schedule line, and nothing has been sent yet.
    await act(async () => { fireEvent.click(byLabel('Clear stopped goal')!) })
    expect(deleteCalls()).toEqual([])
    expect(screen.queryByTestId('auto-nudge-actions')).toBeNull()
    const confirmRow = byLabel('Cancel')!.parentElement!
    expect(Array.from(confirmRow.querySelectorAll('button')).map(b => b.textContent)).toEqual(['Cancel', 'Clear goal for good'])
    expect(screen.getByTestId('auto-nudge-clear-question').textContent).toBe('Remove this goal for good?')
    await act(async () => { fireEvent.click(byLabel('Cancel')!) })
    expect(rowLabels()).toEqual(['Clear stopped goal', PLAY_START])
  })

  it('a record that never carried stopped_reason at all is still Stopped, never Paused', () => {
    // `undefined` is "not known here", and an unknown reason must fail toward
    // the non-resumable reading: only an explicit `manual` earns Resume.
    const loop = makeLoop({ active: false, next_due_ts: 0 })
    delete (loop as Partial<AutoNudgeLoop>).stopped_reason
    renderWith(loop)
    expect(screen.getByTestId('auto-nudge-loop-paused').textContent).toBe('Stopped')
    expect(byLabel(PLAY_RESUME)).toBeNull()
    expect(byLabel(PLAY_START)).toBeTruthy()
  })

  it('NO LOOP: a single accented Play that creates the loop from the form and then fires it on the returned id -- no Stop, no Save', async () => {
    const created = makeLoop({ id: 'l-new', message: 'brand new goal', idle_secs: 60, max_cycles: 0, cycle_count: 0, next_due_ts: Math.floor(Date.now() / 1000) + 60 })
    stubWriteThenFire(created, { ok: true, loop: created })
    const { onChange, onOpenChange } = renderWith(null)
    expectIconRow([PLAY_START])
    // The accent today's "Start loop" text button wore, so the one control on
    // an empty popover still reads as the primary action.
    expect(byLabel(PLAY_START)!.className).toContain('bg-accent')
    expect(byLabel(PLAY_START)!.parentElement).toBe(screen.getByTestId('auto-nudge-loop-controls'))
    for (const name of ['Stop loop', 'Clear stopped goal', 'Save', 'Pause loop', PLAY_RESUME, TRIGGER]) {
      expect(byLabel(name), `${name} rendered with no loop`).toBeNull()
    }
    expect(screen.queryByTestId('auto-nudge-schedule')).toBeNull()
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'brand new goal' } })

    await act(async () => { fireEvent.click(byLabel(PLAY_START)!) })

    // Leg 1: today's create, from the form.
    expect(createCalls()).toHaveLength(1)
    expect(JSON.parse(createCalls()[0][1]!.body!)).toEqual({ slot_key: SLOT, message: 'brand new goal', idle_secs: 60, max_cycles: 0 })
    // Leg 2: the fire on the id the server RETURNED (there was no loop to know
    // before), so the first nudge goes out now rather than after idle_secs.
    expect(fireCalls('l-new')).toHaveLength(1)
    expect(fireCalls('l-new')[0][1]?.method).toBe('POST')
    expect(fireCalls('l-new')[0][1]?.body).toBeUndefined()
    const order = calls().map(c => `${c[1]?.method ?? 'GET'} ${c[0]}`)
    expect(order.indexOf('POST /api/autonudge')).toBeLessThan(order.indexOf('POST /api/autonudge/l-new/fire'))
    expect(onChange).toHaveBeenNthCalledWith(1, created)
    expect((onChange.mock.calls[1][0] as AutoNudgeLoop)).toMatchObject({ id: 'l-new', active: true })
    // Stays open like every other control that fires: the created loop's lane
    // (Stop | Trigger Pause Save) and the due reading appear in place, and a
    // fire refusal needs somewhere to land. Only Save closes the popover.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('a 409 on the no-loop fire leaves the loop CREATED and armed, with the refusal shown inline', async () => {
    const REFUSAL = 'nudge not sent: the agent is still working, so try again when it finishes'
    const created = makeLoop({ id: 'l-new', cycle_count: 0, next_due_ts: Math.floor(Date.now() / 1000) + 90 })
    stubWriteThenFire(created, { ok: false, status: 409, error: REFUSAL })
    const { onChange, onOpenChange } = renderWith(null)

    await act(async () => { fireEvent.click(byLabel(PLAY_START)!) })

    expect(createCalls()).toHaveLength(1)
    expect(fireCalls('l-new')).toHaveLength(1)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(created)
    expect(screen.getByText(REFUSAL)).toBeTruthy()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('the no-loop Play is disabled on an empty goal, like the Save it replaces', () => {
    renderWith(null)
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: '   ' } })
    expect((byLabel(PLAY_START) as HTMLButtonElement).disabled).toBe(true)
  })

  it('Save exists only on a running loop: a plain PATCH of the fields, never active, never a fire, and it closes the popover as it always did', async () => {
    const saved = makeLoop({ message: 'edited' })
    stubPatch(saved)
    const { onChange, onOpenChange } = renderWith(running())
    fireEvent.change(screen.getByLabelText('Goal description'), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(byLabel('Save')!) })

    expect(patchCalls()).toHaveLength(1)
    const body = JSON.parse(patchCalls()[0][1]!.body!)
    expect(body).toEqual({ message: 'edited', idle_secs: 90, max_cycles: 3 })
    // Load-bearing both ways: `active: true` would silently revive a loop
    // another tab paused between render and press, and `active: false` would
    // pause a running one.
    expect(body).not.toHaveProperty('active')
    expect(fireCalls()).toHaveLength(0)
    expect(onChange).toHaveBeenCalledWith(saved)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('Stop on a running loop is a single press with the stop intent; on a paused loop it asks first and the stop intent travels only after the confirm', async () => {
    renderWith(running())
    await act(async () => { fireEvent.click(byLabel('Stop loop')!) })
    expect(deleteCalls()).toEqual(['/api/autonudge/l1?intent=stop'])
    cleanup()

    // A paused loop exists to KEEP its goal, and its Stop removes that goal
    // for good -- so one press asks, exactly as the stopped state's erase
    // does: the row becomes Cancel / Clear goal for good, the question
    // appears on the schedule line, and nothing has been sent.
    renderWith(paused())
    await act(async () => { fireEvent.click(byLabel('Stop loop')!) })
    // Still only the running loop's DELETE from above: the mock outlives cleanup().
    expect(deleteCalls()).toHaveLength(1)
    expect(screen.queryByTestId('auto-nudge-actions')).toBeNull()
    const confirmRow = byLabel('Cancel')!.parentElement!
    expect(Array.from(confirmRow.querySelectorAll('button')).map(b => b.textContent)).toEqual(['Cancel', 'Clear goal for good'])
    expect(screen.getByTestId('auto-nudge-clear-question').textContent).toBe('Remove this goal for good?')
    // Cancel puts the lane back, Play included, takes the question with it,
    // and still nothing was sent.
    await act(async () => { fireEvent.click(byLabel('Cancel')!) })
    expect(rowLabels()).toEqual(['Stop loop', PLAY_RESUME])
    expect(screen.queryByTestId('auto-nudge-clear-question')).toBeNull()
    expect(deleteCalls()).toHaveLength(1)
    // The confirmed press carries the intent the label meant: a paused loop is
    // a live goal being STOPPED, not a terminal record being cleared, so a
    // record that went terminal in between draws the server's 409 instead of
    // a silent erase.
    await act(async () => { fireEvent.click(byLabel('Stop loop')!) })
    await act(async () => { fireEvent.click(byLabel('Clear goal for good')!) })
    expect(deleteCalls()).toEqual(['/api/autonudge/l1?intent=stop', '/api/autonudge/l1?intent=stop'])
    cleanup()

    renderWith(stoppedBy('cycle_cap'))
    await act(async () => { fireEvent.click(byLabel('Clear stopped goal')!) })
    await act(async () => { fireEvent.click(byLabel('Clear goal for good')!) })
    expect(deleteCalls().at(-1)).toBe('/api/autonudge/l1?intent=clear')
  })

  it('surfaces a refused pause inline and keeps the popover open', async () => {
    const REFUSAL = 'audit log unavailable — nudge loop not updated'
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) =>
      init?.method === 'PATCH'
        ? Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: REFUSAL }) })
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())

    await act(async () => { fireEvent.click(byLabel('Pause loop')!) })

    expect(screen.getByText(REFUSAL)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('disables Pause, Trigger, Play and Save but not Stop while writes are disabled', () => {
    // Every control that writes the record follows one gate -- and every
    // fire now writes first, so Trigger and Play are in it; Stop stays
    // reachable for stale state, as before.
    renderWith(running(), vi.fn(), true)
    expect((byLabel('Pause loop') as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel(TRIGGER) as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel('Save') as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel('Stop loop') as HTMLButtonElement).disabled).toBe(false)
    cleanup()
    renderWith(paused(), vi.fn(), true)
    expect((byLabel(PLAY_RESUME) as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel('Stop loop') as HTMLButtonElement).disabled).toBe(false)
    cleanup()
    renderWith(null, vi.fn(), true)
    expect((byLabel(PLAY_START) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('AutoNudgePopover {{STOP_FILE}} token: raw in the textarea, no help line', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('keeps the raw token in the default template and renders NO explanation under the textarea (product owner, 2026-09-17)', () => {
    renderPopover(null)
    // The stored template is untouched: the server substitutes the token at
    // fire time, so the textarea must still carry it.
    expect(goalBox().value).toContain(STOP_FILE_TOKEN)
    // No helper copy anywhere on the surface, for the template, a custom goal
    // carrying the token, or an armed loop with or without a sentinel.
    expect(screen.queryByText(/is filled in when each nudge is sent/i)).toBeNull()
    expect(screen.queryByText(/armed without a stop file/i)).toBeNull()
    expect(goalBox().hasAttribute('aria-describedby')).toBe(false)
    cleanup()
    renderPopover(makeLoop({ message: `Keep going. To halt, create ${STOP_FILE_TOKEN}`, stop_sentinel_path: '' }))
    expect(screen.queryByText(/is filled in when each nudge is sent/i)).toBeNull()
    expect(screen.queryByText(/armed without a stop file/i)).toBeNull()
    cleanup()
    renderPopover(makeLoop({ message: `Keep going. To halt, create ${STOP_FILE_TOKEN}`, stop_sentinel_path: '/home/someone/.stop-chat-1-100' }))
    expect(screen.queryByText(/is filled in when each nudge is sent/i)).toBeNull()
    expect(screen.queryByText(/\.stop-chat-1-100/)).toBeNull()
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
})
