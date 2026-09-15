/**
 * The Crew Companion panel's secondary views, exercised through their real bridge
 * seam.
 *
 * `PanelViews.tsx` holds four exported surfaces and one private one: the back row
 * (`ViewHeader`), the full reminder list (`AllRemindersView`), the settings body
 * (`SettingsView`, which owns the private `Toggle`), and the render guard
 * (`ViewBoundary`). Every one of them talks to the desktop app through the module
 * scoped `api = petBridge`, so the bridge is the only thing doubled here — the
 * components, the shared `ReminderInput` composer, the skin context defaults and the
 * real i18n catalog all run for real.
 *
 * The clock is faked and pinned: `AllRemindersView` builds every row label from
 * `new Date()` at render time, so a real clock would make "30 min from now" a
 * flake. `shouldAdvanceTime` keeps the clock moving so `waitFor` and the promise
 * driven effects behave as they do with real timers, and the teardown clears any
 * callback that would otherwise fire after the DOM is gone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'

import type { Reminder } from '../apps/crew-companion/types'

// ── Bridge double ──────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => ({
  api: {
    remindersList: vi.fn(),
    remindersRemove: vi.fn(),
    getCrewCompanionConfig: vi.fn(),
    updateConfig: vi.fn(),
    onConfigUpdated: vi.fn(),
  },
}))

vi.mock('../apps/crew-companion/petBridge', () => ({
  petBridge: mocks.api,
  galleryApi: mocks.api,
}))

const api = mocks.api

// Imported AFTER the mock is registered, so the module level `api` binding in
// PanelViews resolves to the double rather than the real bridge.
const { ViewHeader, AllRemindersView, SettingsView, ViewBoundary } =
  await import('../apps/crew-companion/PanelViews')

// ── Fixtures ───────────────────────────────────────────────────────────────

/** Pinned so every relative label below is arithmetic, not weather. */
const NOW = new Date('2026-08-12T00:00:00.000Z')

const at = (msFromNow: number) => new Date(NOW.getTime() + msFromNow).toISOString()

const reminder = (over: Partial<Reminder> & Pick<Reminder, 'id' | 'text' | 'fireAt'>): Reminder => ({
  recurrence: null,
  ...over,
})

/** The unsubscribe handed back by `onConfigUpdated`, asserted on unmount. */
let off: ReturnType<typeof vi.fn>
/** The broadcast callback the view registers, so a test can fire `config:updated`. */
let broadcast: () => void

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
  vi.clearAllMocks()

  off = vi.fn()
  broadcast = () => {}
  api.remindersList.mockResolvedValue([])
  api.remindersRemove.mockResolvedValue(true)
  api.getCrewCompanionConfig.mockResolvedValue({})
  api.updateConfig.mockResolvedValue(true)
  api.onConfigUpdated.mockImplementation((cb: () => void) => {
    broadcast = cb
    return off
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
})

// ── Query helpers ──────────────────────────────────────────────────────────

/** Every reminder row, in document order, as normalized text. */
const rowTexts = () =>
  screen
    .getAllByRole('button', { name: 'Remove reminder' })
    .map((btn) => (btn.parentElement?.textContent ?? '').replace(/\s+/g, ' ').trim())

/** The two settings switches, break first and session second — that is render order. */
const switches = () => screen.getAllByRole('switch') as HTMLInputElement[]

const customField = () => screen.getByLabelText('Custom minutes') as HTMLInputElement

const preset = (mins: number) => screen.getByRole('button', { name: String(mins) })

// ── ViewHeader ─────────────────────────────────────────────────────────────

describe('ViewHeader — the only way out besides Escape', () => {
  it('renders the title beside a labelled back button', () => {
    render(<ViewHeader title="All reminders" onBack={vi.fn()} />)

    expect(screen.getByText('All reminders')).toBeInTheDocument()
    const back = screen.getByRole('button', { name: 'Back' })
    // The title is a drag region, so the button opts out or the click is eaten.
    expect(back).toHaveAttribute('title', 'Back')
  })

  it('calls onBack once per click', () => {
    const onBack = vi.fn()
    render(<ViewHeader title="Settings" onBack={onBack} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it('survives the hover in and hover out handlers and still fires onBack', () => {
    // The handlers repaint the button from the skin. What matters for behaviour is
    // that neither one throws and the control keeps working afterwards — the
    // resolved colours are CSS custom properties, not values worth asserting.
    const onBack = vi.fn()
    render(<ViewHeader title="Settings" onBack={onBack} />)
    const back = screen.getByRole('button', { name: 'Back' })

    fireEvent.mouseEnter(back)
    fireEvent.mouseLeave(back)

    fireEvent.click(back)
    expect(onBack).toHaveBeenCalledTimes(1)
  })
})

// ── AllRemindersView ───────────────────────────────────────────────────────

describe('AllRemindersView — the full list', () => {
  it('says it is reading while the list is still in flight', () => {
    // A promise that never settles: the null state is the thing under test.
    api.remindersList.mockReturnValue(new Promise<Reminder[]>(() => {}))
    render(<AllRemindersView />)

    expect(screen.getByText('Reading…')).toBeInTheDocument()
    // The composer is offered even before the list arrives.
    expect(screen.getByLabelText('Add a reminder in your own words')).toBeInTheDocument()
  })

  it('offers the empty state with a hint to type above, not to navigate back', async () => {
    render(<AllRemindersView />)

    expect(await screen.findByText('Nothing scheduled')).toBeInTheDocument()
    expect(screen.getByText('Type what you want to remember above.')).toBeInTheDocument()
  })

  it('treats a failed read as an empty list rather than a broken screen', async () => {
    api.remindersList.mockRejectedValue(new Error('bridge is down'))
    render(<AllRemindersView />)

    expect(await screen.findByText('Nothing scheduled')).toBeInTheDocument()
  })

  it('treats a bridge that answers with nothing as an empty list', async () => {
    // Not the same path as a rejection: this one resolves, with no list in it.
    api.remindersList.mockResolvedValue(undefined)
    render(<AllRemindersView />)

    expect(await screen.findByText('Nothing scheduled')).toBeInTheDocument()
  })

  it('sinks already-fired one-offs below pending ones and sorts the rest by time', async () => {
    api.remindersList.mockResolvedValue([
      reminder({ id: 'd', text: 'call mom', fireAt: at(3 * 86_400_000) }),
      reminder({ id: 'a', text: 'watered the plants', fireAt: at(-2 * 3_600_000), done: true }),
      reminder({ id: 'c', text: 'stretch', fireAt: at(2 * 3_600_000) }),
      reminder({ id: 'b', text: 'stand up', fireAt: at(30 * 60_000) }),
    ])
    render(<AllRemindersView />)

    await screen.findByText('stand up')
    const order = rowTexts()
    expect(order).toHaveLength(4)
    expect(order[0]).toContain('stand up')
    expect(order[1]).toContain('stretch')
    expect(order[2]).toContain('call mom')
    // The fired one is last, still visible — seeing what just fired is the point.
    expect(order[3]).toContain('watered the plants')
  })

  it('labels each row through the shared time formatter', async () => {
    api.remindersList.mockResolvedValue([
      reminder({ id: 'b', text: 'stand up', fireAt: at(30 * 60_000) }),
    ])
    render(<AllRemindersView />)

    // Under an hour is relative only.
    expect(await screen.findByText('30 min from now')).toBeInTheDocument()
  })

  it('renders a repeat pill per recurrence granularity', async () => {
    api.remindersList.mockResolvedValue([
      reminder({
        id: 'min', text: 'sip water', fireAt: at(10 * 60_000),
        recurrence: { everyMinutes: 45 },
      }),
      reminder({
        id: 'hr', text: 'stretch', fireAt: at(20 * 60_000),
        recurrence: { everyMinutes: 120 },
      }),
      reminder({
        id: 'day', text: 'stand up', fireAt: at(30 * 60_000),
        recurrence: { everyMinutes: 2880 },
      }),
      reminder({
        id: 'daily', text: 'call mom', fireAt: at(40 * 60_000),
        recurrence: { everyMinutes: 1440 },
      }),
    ])
    render(<AllRemindersView />)

    await screen.findByText('sip water')
    const order = rowTexts()
    // Minutes, hours and days each go through the locale's own narrow unit; only
    // the exact-day case gets its own word.
    expect(order[0]).toMatch(/every\s*45\s*m/)
    expect(order[1]).toMatch(/every\s*2\s*h/)
    expect(order[2]).toMatch(/every\s*2\s*d/)
    expect(order[3]).toMatch(/daily/)
    // "every 2d" would be wrong for the exact-daily case, so prove it is absent.
    expect(order[3]).not.toMatch(/every\s*1\s*d/)
  })

  it('omits the pill entirely for a one-off', async () => {
    api.remindersList.mockResolvedValue([
      reminder({ id: 'one', text: 'buy milk', fireAt: at(90 * 60_000) }),
    ])
    render(<AllRemindersView />)

    await screen.findByText('buy milk')
    expect(rowTexts()[0]).not.toMatch(/every/)
  })

  it('removes a row through the bridge and re-reads the list afterwards', async () => {
    api.remindersList.mockResolvedValue([
      reminder({ id: 'r-1', text: 'stretch', fireAt: at(3_600_000) }),
    ])
    render(<AllRemindersView />)
    await screen.findByText('stretch')
    expect(api.remindersList).toHaveBeenCalledTimes(1)

    api.remindersList.mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Remove reminder' }))

    await waitFor(() => expect(api.remindersRemove).toHaveBeenCalledWith('r-1'))
    // The row is gone because the list re-read, not because it was spliced locally.
    expect(await screen.findByText('Nothing scheduled')).toBeInTheDocument()
    expect(api.remindersList).toHaveBeenCalledTimes(2)
  })

  it('re-reads the list when the composer saves something', async () => {
    const onAdd = vi.fn().mockResolvedValue(true)
    render(<AllRemindersView onAdd={onAdd} />)
    await screen.findByText('Nothing scheduled')
    expect(api.remindersList).toHaveBeenCalledTimes(1)

    api.remindersList.mockResolvedValue([
      reminder({ id: 'new', text: 'stretch', fireAt: at(3_600_000) }),
    ])
    const input = screen.getByLabelText('Add a reminder in your own words') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'stretch in 20 minutes' } })
    fireEvent.submit(input.closest('form')!)

    await waitFor(() => expect(onAdd).toHaveBeenCalled())
    expect(await screen.findByText('stretch')).toBeInTheDocument()
    expect(api.remindersList).toHaveBeenCalledTimes(2)
  })

  it('does not re-read when the save was refused', async () => {
    const onAdd = vi.fn().mockResolvedValue(false)
    render(<AllRemindersView onAdd={onAdd} />)
    await screen.findByText('Nothing scheduled')

    const input = screen.getByLabelText('Add a reminder in your own words') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'stretch in 20 minutes' } })
    fireEvent.submit(input.closest('form')!)

    await waitFor(() => expect(onAdd).toHaveBeenCalled())
    // One read, from mount. A refused write has nothing new to show.
    expect(api.remindersList).toHaveBeenCalledTimes(1)
  })

  it('still renders the composer when no onAdd was threaded through', async () => {
    render(<AllRemindersView />)
    await screen.findByText('Nothing scheduled')

    const input = screen.getByLabelText('Add a reminder in your own words') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'stretch in 20 minutes' } })
    fireEvent.submit(input.closest('form')!)

    // Nothing to persist to, so the list never re-reads and nothing throws.
    await waitFor(() => expect(api.remindersList).toHaveBeenCalledTimes(1))
  })
})

// ── SettingsView ───────────────────────────────────────────────────────────

describe('SettingsView — reads once, then follows the broadcast', () => {
  it('shows both switches on and the stored cadence selected', async () => {
    api.getCrewCompanionConfig.mockResolvedValue({
      breakNudgesEnabled: true,
      sessionNotificationsEnabled: true,
      breakReminderMins: 60,
    })
    render(<SettingsView />)

    await waitFor(() => expect(preset(60)).toHaveAttribute('aria-pressed', 'true'))
    expect(preset(45)).toHaveAttribute('aria-pressed', 'false')
    expect(switches()[0].checked).toBe(true)
    expect(switches()[1].checked).toBe(true)
    // The caveat is stated once for the whole section, not inside a toggle hint.
    expect(screen.getByText(/always notify/i)).toBeInTheDocument()
  })

  it('keeps its defaults when the config answers with nothing usable', async () => {
    api.getCrewCompanionConfig.mockResolvedValue({
      breakNudgesEnabled: 'yes',
      sessionNotificationsEnabled: null,
      breakReminderMins: '90',
    })
    render(<SettingsView />)

    await waitFor(() => expect(api.getCrewCompanionConfig).toHaveBeenCalled())
    // Wrong types are ignored rather than coerced, so the 45 default survives.
    expect(preset(45)).toHaveAttribute('aria-pressed', 'true')
    expect(switches()[0].checked).toBe(true)
    expect(switches()[1].checked).toBe(true)
  })

  it('survives a null config and a failed read', async () => {
    api.getCrewCompanionConfig.mockResolvedValue(null)
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))

    cleanup()
    api.getCrewCompanionConfig.mockRejectedValue(new Error('bridge is down'))
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))
  })

  it('hides the cadence row while break nudges are off', async () => {
    api.getCrewCompanionConfig.mockResolvedValue({ breakNudgesEnabled: false })
    render(<SettingsView />)

    await waitFor(() => expect(switches()[0].checked).toBe(false))
    // The interval exists only because the toggle above it is on.
    expect(screen.queryByLabelText('Custom minutes')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '45' })).not.toBeInTheDocument()
  })

  it('writes the break toggle through and folds the cadence row away', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())

    fireEvent.click(switches()[0])

    expect(api.updateConfig).toHaveBeenCalledWith({ breakNudgesEnabled: false })
    await waitFor(() => expect(screen.queryByLabelText('Custom minutes')).not.toBeInTheDocument())
  })

  it('writes the session toggle through without touching the cadence row', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())

    fireEvent.click(switches()[1])

    expect(api.updateConfig).toHaveBeenCalledWith({ sessionNotificationsEnabled: false })
    expect(screen.getByLabelText('Custom minutes')).toBeInTheDocument()
  })

  it('selects a preset and persists it', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))

    fireEvent.click(preset(90))

    expect(api.updateConfig).toHaveBeenCalledWith({ breakReminderMins: 90 })
    expect(preset(90)).toHaveAttribute('aria-pressed', 'true')
    expect(preset(45)).toHaveAttribute('aria-pressed', 'false')
  })

  it('leaves the custom field blank while a preset is selected', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))

    expect(customField().value).toBe('')
  })

  it('shows the live value in the custom field when the interval is not a preset', async () => {
    api.getCrewCompanionConfig.mockResolvedValue({ breakReminderMins: 200 })
    render(<SettingsView />)

    await waitFor(() => expect(customField().value).toBe('200'))
    // A custom value means no preset claims to be the current one.
    for (const m of [30, 45, 60, 90]) {
      expect(preset(m)).toHaveAttribute('aria-pressed', 'false')
    }
  })

  it('seeds the draft on focus and commits it on blur', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())
    const field = customField()

    field.focus()
    // Focused on a preset value, so the field opens empty rather than pre-filled.
    expect(field.value).toBe('')

    fireEvent.change(field, { target: { value: '200' } })
    expect(field.value).toBe('200')

    fireEvent.blur(field)
    expect(api.updateConfig).toHaveBeenCalledWith({ breakReminderMins: 200 })
    await waitFor(() => expect(customField().value).toBe('200'))
  })

  it('seeds the draft with the live value when that value is custom', async () => {
    api.getCrewCompanionConfig.mockResolvedValue({ breakReminderMins: 200 })
    render(<SettingsView />)
    await waitFor(() => expect(customField().value).toBe('200'))

    customField().focus()
    // Editing an existing custom interval starts from it, not from blank.
    expect(customField().value).toBe('200')
  })

  it('clamps a below-floor and an above-ceiling entry instead of storing them raw', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())

    fireEvent.change(customField(), { target: { value: '2' } })
    fireEvent.blur(customField())
    expect(api.updateConfig).toHaveBeenLastCalledWith({ breakReminderMins: 5 })

    fireEvent.change(customField(), { target: { value: '9999' } })
    fireEvent.blur(customField())
    expect(api.updateConfig).toHaveBeenLastCalledWith({ breakReminderMins: 480 })
  })

  it('leaves the stored interval alone when the entry is not a number', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))

    fireEvent.change(customField(), { target: { value: 'soonish' } })
    fireEvent.blur(customField())

    // A bad value must not silently reset the setting.
    expect(api.updateConfig).not.toHaveBeenCalled()
    expect(preset(45)).toHaveAttribute('aria-pressed', 'true')
    expect(customField().value).toBe('')
  })

  it('commits the custom entry on Enter', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())
    const field = customField()

    field.focus()
    fireEvent.change(field, { target: { value: '75' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    // Enter blurs the field, and the blur is what writes.
    await waitFor(() => expect(api.updateConfig).toHaveBeenCalledWith({ breakReminderMins: 75 }))
  })

  it('ignores other keys while typing', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(customField()).toBeInTheDocument())
    const field = customField()

    field.focus()
    fireEvent.change(field, { target: { value: '75' } })
    fireEvent.keyDown(field, { key: 'a' })

    expect(api.updateConfig).not.toHaveBeenCalled()
    expect(field.value).toBe('75')
  })

  it('re-reads on a config broadcast and drops a half-typed draft', async () => {
    render(<SettingsView />)
    await waitFor(() => expect(preset(45)).toHaveAttribute('aria-pressed', 'true'))

    customField().focus()
    fireEvent.change(customField(), { target: { value: '77' } })
    expect(customField().value).toBe('77')

    // The dashboard app page edits the same settings; this window only hears it.
    api.getCrewCompanionConfig.mockResolvedValue({ breakReminderMins: 90 })
    await act(async () => { broadcast() })

    await waitFor(() => expect(preset(90)).toHaveAttribute('aria-pressed', 'true'))
    // The draft described a value the user is no longer setting.
    expect(customField().value).toBe('')
  })

  it('unsubscribes from the broadcast on unmount', async () => {
    const view = render(<SettingsView />)
    await waitFor(() => expect(api.onConfigUpdated).toHaveBeenCalled())

    view.unmount()
    expect(off).toHaveBeenCalledTimes(1)
  })
})

// ── ViewBoundary ───────────────────────────────────────────────────────────

describe('ViewBoundary — a visible failure beats a blank window', () => {
  const Boom: React.FC = () => { throw new Error('render exploded') }

  let errorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // React logs the caught error itself; the boundary logs it again on purpose.
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    errorSpy.mockRestore()
  })

  it('renders its children untouched while nothing is wrong', () => {
    render(
      <ViewBoundary onBack={vi.fn()} label="This screen didn't load." back="Back">
        <div>all reminders body</div>
      </ViewBoundary>,
    )

    expect(screen.getByText('all reminders body')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
  })

  it('replaces a failed child with a labelled way out, and logs the cause', () => {
    const onBack = vi.fn()
    render(
      <ViewBoundary onBack={onBack} label="This screen didn't load." back="Back">
        <Boom />
      </ViewBoundary>,
    )

    expect(screen.getByText("This screen didn't load.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledTimes(1)

    // The cause is not swallowed — a blank panel with no log was the old failure.
    expect(errorSpy).toHaveBeenCalledWith(
      '[panel] view failed to render:',
      expect.any(Error),
    )
  })
})
