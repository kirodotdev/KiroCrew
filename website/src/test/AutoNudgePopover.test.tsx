import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { __resetForTests, loadGoalDraft, saveGoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-1-100'

function renderPopover(loop: AutoNudgeLoop | null) {
  return render(
    <AutoNudgePopover
      slotKey={SLOT}
      loop={loop}
      open={true}
      onOpenChange={() => {}}
      onChange={() => {}}
    />,
  )
}

const makeLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'active loop goal',
  idle_secs: 90, max_cycles: 3, cycle_count: 1, active: true, last_fire_ts: 0, ...over,
})

describe('AutoNudgePopover goal persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    // Popover only calls fetch on Save/Stop; stub so nothing escapes the test.
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

    // 2. The loop is stopped elsewhere → ChatPage passes loop={null} on re-open.
    //    Before the fix this fell back to the default template.
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
    // default template + 60, matching pre-fix behavior — regression guard for
    // the || → ?? operator change flagged in review.
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
    // Pre-fix the per-keystroke coercion snapped an emptied field straight back
    // to 60 (leading digit stuck). Now the empty edit is allowed as-typed...
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
    const body = JSON.parse((fetch as unknown as { mock: { calls: any[][] } }).mock.calls[0][1].body)
    expect(body.idle_secs).toBe(45)
  })
})
