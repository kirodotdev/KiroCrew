/**
 * The Crew Companion dashboard sections and the desktop panel card.
 *
 * All three are conditional surfaces whose interesting states only appear when
 * the desktop app is unreachable, a write fails, or a reminder repeats:
 *  - `SettingsSection` renders its controls even offline (it must say WHY, not
 *    hide what it controls) and owns the custom-interval draft, whose whole point
 *    is that an emptied field does not snap back to the stored number;
 *  - `RemindersSection` must never invent a time and must never discard the
 *    user's draft on an unconfirmed write;
 *  - `PanelCard` swaps its whole body for a secondary view, and reveals per-row
 *    Skip/Remove only where they mean something.
 *
 * `PanelViews` is stubbed: the real views fetch on mount, and every assertion
 * here is about the card's own decisions.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import SettingsSection from '../apps/crew-companion/SettingsSection'
import RemindersSection from '../apps/crew-companion/RemindersSection'
import { BREAK_PRESETS, BREAK_MAX_MINS, BREAK_MIN_MINS } from '../apps/crew-companion/constants'
import type { RemindersPayload } from '../apps/crew-companion/types'

vi.mock('../apps/crew-companion/PanelViews', () => ({
  ViewHeader: ({ title, onBack }: { title: string; onBack: () => void }) => (
    <button type="button" data-testid="view-back" onClick={onBack}>{title}</button>
  ),
  ViewBoundary: ({ children }: { children: React.ReactNode }) => <div data-testid="boundary">{children}</div>,
  AllRemindersView: () => <div data-testid="view-all" />,
  SettingsView: () => <div data-testid="view-settings" />,
}))

const { PanelCard } = await import('../apps/crew-companion/PanelCard')

function payload(over: Partial<RemindersPayload> = {}): RemindersPayload {
  return {
    reminders: [],
    breakNudgesEnabled: false,
    sessionNotificationsEnabled: false,
    breakReminderMins: 45,
    language: 'en',
    present: true,
    ...over,
  }
}

describe('crew-companion/SettingsSection', () => {
  it('renders its controls disabled, with a reason, when the app is unreachable', () => {
    const { container } = render(
      <SettingsSection rem={null} remError="offline" onCfg={vi.fn()} customMins={null} setCustomMins={vi.fn()} />,
    )
    expect(container.querySelector('.cc-hint')).not.toBeNull()
    const switches = screen.getAllByRole('switch')
    expect(switches.length).toBe(2)
    for (const s of switches) expect((s as HTMLButtonElement).disabled).toBe(true)
    // How-often is hidden while nudges are off.
    expect(container.querySelector('.cc-every')).toBeNull()
  })

  it('says nothing about being offline while the first fetch is merely in flight', () => {
    const { container } = render(
      <SettingsSection rem={null} remError={null} onCfg={vi.fn()} customMins={null} setCustomMins={vi.fn()} />,
    )
    expect(container.querySelector('.cc-hint')).toBeNull()
  })

  it('reports both toggles through their own patch keys', () => {
    const onCfg = vi.fn()
    render(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true, sessionNotificationsEnabled: false })}
        remError={null}
        onCfg={onCfg}
        customMins={null}
        setCustomMins={vi.fn()}
      />,
    )
    const [breaks, session] = screen.getAllByRole('switch')
    fireEvent.click(breaks)
    fireEvent.click(session)
    expect(onCfg).toHaveBeenNthCalledWith(1, { breakNudgesEnabled: false })
    expect(onCfg).toHaveBeenNthCalledWith(2, { sessionNotificationsEnabled: true })
  })

  it('offers the presets once nudges are on, and clears any custom draft on pick', () => {
    const onCfg = vi.fn()
    const setCustomMins = vi.fn()
    const { container } = render(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true, breakReminderMins: BREAK_PRESETS[0] })}
        remError={null}
        onCfg={onCfg}
        customMins={null}
        setCustomMins={setCustomMins}
      />,
    )
    const pills = Array.from(container.querySelectorAll('.cc-pill')) as HTMLButtonElement[]
    expect(pills.map((p) => p.textContent)).toEqual(BREAK_PRESETS.map(String))
    // The stored value is the pressed one.
    expect(pills[0].getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(pills[2])
    expect(setCustomMins).toHaveBeenCalledWith(null)
    expect(onCfg).toHaveBeenCalledWith({ breakReminderMins: BREAK_PRESETS[2] })
  })

  it('shows a non-preset interval in the field and marks it custom', () => {
    const { container } = render(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true, breakReminderMins: 37 })}
        remError={null}
        onCfg={vi.fn()}
        customMins={null}
        setCustomMins={vi.fn()}
      />,
    )
    const input = container.querySelector('.cc-num') as HTMLInputElement
    expect(input.value).toBe('37')
    expect(input.className).toContain('is-custom')
    expect(input.min).toBe(String(BREAK_MIN_MINS))
    expect(input.max).toBe(String(BREAK_MAX_MINS))
  })

  it('keeps an emptied field empty and clamps only what the user commits', () => {
    const onCfg = vi.fn()
    const setCustomMins = vi.fn()
    const { container, rerender } = render(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true, breakReminderMins: 37 })}
        remError={null}
        onCfg={onCfg}
        customMins={null}
        setCustomMins={setCustomMins}
      />,
    )
    const input = container.querySelector('.cc-num') as HTMLInputElement
    fireEvent.focus(input)
    expect(setCustomMins).toHaveBeenCalledWith('37')
    fireEvent.change(input, { target: { value: '9999' } })
    expect(setCustomMins).toHaveBeenLastCalledWith('9999')

    // Committed as the draft the parent now holds — clamped to the ceiling.
    rerender(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true, breakReminderMins: 37 })}
        remError={null}
        onCfg={onCfg}
        customMins="9999"
        setCustomMins={setCustomMins}
      />,
    )
    fireEvent.blur(container.querySelector('.cc-num') as HTMLInputElement)
    expect(setCustomMins).toHaveBeenLastCalledWith(null)
    expect(onCfg).toHaveBeenCalledWith({ breakReminderMins: BREAK_MAX_MINS })
  })

  it('writes nothing when the committed draft is not a number', () => {
    const onCfg = vi.fn()
    const { container } = render(
      <SettingsSection
        rem={payload({ breakNudgesEnabled: true })}
        remError={null}
        onCfg={onCfg}
        customMins=""
        setCustomMins={vi.fn()}
      />,
    )
    const input = container.querySelector('.cc-num') as HTMLInputElement
    // Enter blurs, and the blur commit rejects an empty draft.
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.blur(input)
    expect(onCfg).not.toHaveBeenCalled()
    // A key that is not Enter is left alone.
    fireEvent.keyDown(input, { key: 'x' })
    expect(onCfg).not.toHaveBeenCalled()
  })
})

describe('crew-companion/RemindersSection', () => {
  const base = {
    onSkip: vi.fn(),
    onRemove: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  function renderSection(rem: RemindersPayload | null, remError: string | null, onAdd = vi.fn()) {
    const utils = render(
      <RemindersSection rem={rem} remError={remError} onAdd={onAdd} onSkip={base.onSkip} onRemove={base.onRemove} />,
    )
    const input = utils.container.querySelector('.cc-add-input') as HTMLInputElement
    const form = utils.container.querySelector('.cc-add') as HTMLFormElement
    return { input, form, onAdd, ...utils }
  }

  it('disables the add box and reports offline when the app is unreachable', () => {
    const { input, container } = renderSection(null, 'offline')
    expect(input.disabled).toBe(true)
    expect(container.querySelector('.cc-muted')).not.toBeNull()
  })

  it('distinguishes loading from an empty list', () => {
    const loading = renderSection(null, null).container.textContent
    const empty = renderSection(payload(), null).container.textContent
    expect(loading).not.toBe(empty)
  })

  it('submits nothing for a blank draft', async () => {
    const { form, onAdd } = renderSection(payload(), null)
    fireEvent.submit(form)
    expect(onAdd).not.toHaveBeenCalled()
  })

  it('refuses to invent a time and says so', async () => {
    const { input, form, onAdd, container } = renderSection(payload(), null)
    fireEvent.change(input, { target: { value: 'zzz no time here' } })
    fireEvent.submit(form)
    await waitFor(() => expect(container.querySelector('.cc-hint')).not.toBeNull())
    expect(onAdd).not.toHaveBeenCalled()
    // Typing again clears the note.
    fireEvent.change(input, { target: { value: 'zzz other' } })
    expect(container.querySelector('.cc-hint')).toBeNull()
  })

  it('clears the draft only once the write landed', async () => {
    const onAdd = vi.fn().mockResolvedValue(false)
    const { input, form } = renderSection(payload(), null, onAdd)
    fireEvent.change(input, { target: { value: 'zzz in 10 minutes' } })
    fireEvent.submit(form)
    await waitFor(() => expect(onAdd).toHaveBeenCalled())
    expect(onAdd.mock.calls[0][0]).toContain('zzz')
    expect(typeof onAdd.mock.calls[0][1]).toBe('string')
    // Rejected write: the typed text survives.
    expect(input.value).toBe('zzz in 10 minutes')

    onAdd.mockResolvedValue(true)
    fireEvent.submit(form)
    await waitFor(() => expect(input.value).toBe(''))
  })

  it('counts only the outstanding reminders', () => {
    const { container } = renderSection(
      payload({
        reminders: [
          { id: 'a', text: 'zz-a', fireAt: new Date(Date.now() + 60_000).toISOString(), recurrence: null },
          { id: 'b', text: 'zz-b', fireAt: new Date(Date.now() - 60_000).toISOString(), recurrence: null, done: true },
        ],
      }),
      null,
    )
    expect(container.querySelectorAll('.cc-row').length).toBe(2)
    // The done row is struck through; the outstanding one is not.
    expect(container.querySelectorAll('.cc-rem-done').length).toBe(2)
  })

  it('offers Skip only where there is a next occurrence, and always Remove', () => {
    const { container } = renderSection(
      payload({
        reminders: [
          {
            id: 'rec',
            text: 'zz-recurring',
            fireAt: new Date(Date.now() + 3_600_000).toISOString(),
            recurrence: { everyMinutes: 60 },
          },
          { id: 'once', text: 'zz-once', fireAt: new Date(Date.now() + 60_000).toISOString(), recurrence: null },
        ],
      }),
      null,
    )
    const removes = container.querySelectorAll('.cc-icon-btn.is-remove')
    const skips = Array.from(container.querySelectorAll('.cc-icon-btn')).filter(
      (b) => !b.classList.contains('is-remove'),
    )
    expect(removes.length).toBe(2)
    expect(skips.length).toBe(1)
    fireEvent.click(skips[0])
    // Rows are chronological, so the sooner one-time reminder is listed first.
    fireEvent.click(removes[0])
    expect(base.onSkip).toHaveBeenCalledWith('rec')
    expect(base.onRemove).toHaveBeenCalledWith('once')
  })
})

describe('crew-companion/PanelCard', () => {
  const item = {
    id: 'zz1',
    text: 'zz-text',
    relLabel: 'zz-rel',
    absLabel: 'zz-abs',
    recurring: true,
  }

  /** The UP NEXT rows — addressed by their own flex geometry, not by copy. */
  function rowsOf(container: HTMLElement) {
    return Array.from(container.querySelectorAll('div[style*="gap: 7px"]')) as HTMLElement[]
  }

  it('renders the nothing-scheduled state with no items', () => {
    const { container } = render(<PanelCard upNext={[]} breakMins={30} sessionOn />)
    // No rows at all, so no per-row controls either.
    expect(rowsOf(container).length).toBe(0)
  })

  it('reveals Skip and Remove for a hovered recurring row only', () => {
    const onSkip = vi.fn()
    const onRemove = vi.fn()
    const { container } = render(
      <PanelCard
        upNext={[item, { id: 'zz2', text: 'zz-two', relLabel: 'zz-rel2', tone: 'ok' }]}
        breakMins={30}
        sessionOn={false}
        onSkip={onSkip}
        onRemove={onRemove}
      />,
    )
    const rows = rowsOf(container)
    expect(rows.length).toBe(2)
    // The recurring row carries Skip AND Remove; the one-time row only Remove —
    // "not this time" needs a next time to move to.
    const first = Array.from(rows[0].querySelectorAll('button')) as HTMLButtonElement[]
    const second = Array.from(rows[1].querySelectorAll('button')) as HTMLButtonElement[]
    expect(first.length).toBe(2)
    expect(second.length).toBe(1)
    fireEvent.click(first[0])
    fireEvent.click(first[1])
    fireEvent.click(second[0])
    expect(onSkip).toHaveBeenCalledWith('zz1')
    expect(onRemove).toHaveBeenNthCalledWith(1, 'zz1')
    expect(onRemove).toHaveBeenNthCalledWith(2, 'zz2')
  })

  it('tracks which row the cursor is on', () => {
    const { container } = render(<PanelCard upNext={[item]} breakMins={30} sessionOn />)
    const row = rowsOf(container)[0]
    fireEvent.mouseEnter(row)
    fireEvent.mouseLeave(row)
    expect(row).toBeTruthy()
  })

  it('runs the breathing invite, see-all and settings doors', () => {
    const onBreathe = vi.fn()
    const onSeeAll = vi.fn()
    const onSettings = vi.fn()
    const { container } = render(
      <PanelCard
        upNext={[]}
        breakMins={30}
        sessionOn
        onBreathe={onBreathe}
        onSeeAll={onSeeAll}
        onSettings={onSettings}
      />,
    )
    const plain = Array.from(container.querySelectorAll('button')).filter(
      (b) => !b.getAttribute('aria-label'),
    ) as HTMLButtonElement[]
    for (const b of plain) fireEvent.click(b)
    expect(onBreathe).toHaveBeenCalled()
    expect(onSeeAll).toHaveBeenCalled()
    expect(onSettings).toHaveBeenCalled()
  })

  it('closes from the ✕, which tints on hover and opts out of the drag strip', () => {
    const onClose = vi.fn()
    const { container } = render(
      <PanelCard upNext={[]} breakMins={30} sessionOn onClose={onClose} openSide="left" />,
    )
    const close = container.querySelector('button[aria-label]') as HTMLButtonElement
    // The hit area, not the glyph: a 20px round box dropped its four corners
    // through to the element behind and sat under the 24px minimum target size.
    expect(close.style.width).toBe('28px')
    expect(close.style.height).toBe('28px')
    // The hover tint is applied imperatively; happy-dom does not resolve the
    // theme `var()` it assigns, so this exercises the handlers rather than
    // asserting a computed colour.
    fireEvent.mouseEnter(close)
    fireEvent.mouseLeave(close)
    fireEvent.click(close)
    expect(onClose).toHaveBeenCalledTimes(1)
    // The spring's origin tracks the side the panel opened on.
    expect((container.querySelector('.cc-card') as HTMLElement).style.transformOrigin).toContain('right')
  })

  it('replaces the whole body with a secondary view and comes back', () => {
    const onBack = vi.fn()
    const all = render(<PanelCard upNext={[item]} breakMins={30} sessionOn view="all" onBack={onBack} />)
    expect(all.container.querySelector('.cc-cascade')).toBeNull()
    expect(all.getByTestId('view-all')).toBeTruthy()
    fireEvent.click(all.getByTestId('view-back'))
    expect(onBack).toHaveBeenCalled()
    all.unmount()

    const settings = render(<PanelCard upNext={[]} breakMins={30} sessionOn view="settings" />)
    expect(settings.getByTestId('view-settings')).toBeTruthy()
    // No onBack supplied — the header must still be clickable without throwing.
    expect(() => fireEvent.click(settings.getByTestId('view-back'))).not.toThrow()
  })
})
