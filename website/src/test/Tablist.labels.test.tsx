// `labels="active"` is the crewmate profile card's rail: five icon tabs in a
// column a third of the row wide, where five words would not fit. The selected
// tab shows its word; the others are their icon alone. What this file pins is
// that the word is HIDDEN, never dropped — the accessible name and the tooltip
// keep it, so a screen-reader user and a hovering pointer still learn what each
// icon is — and that the default (`always`) is unchanged for every other rail.
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import Tablist, { type TablistTab } from '../components/Tablist'

type Key = 'profile' | 'schedule' | 'goals'

const TABS: Array<TablistTab<Key>> = [
  { key: 'profile', label: 'Profile', icon: <svg data-testid="icon-profile" /> },
  { key: 'schedule', label: 'Schedules', icon: <svg data-testid="icon-schedule" /> },
  { key: 'goals', label: 'Goals', icon: <svg data-testid="icon-goals" />, count: 2 },
]

beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
})

/** The label span that is laid out (not `sr-only`) inside a tab, if any. */
const visibleLabel = (tab: HTMLElement) =>
  Array.from(tab.querySelectorAll('span')).find(
    (s) => s.textContent?.trim() && !s.classList.contains('sr-only') && s.getAttribute('aria-hidden') !== 'true',
  )

describe('Tablist labels="active"', () => {
  it('shows the word on the selected tab only; the others keep it for assistive tech', () => {
    render(<Tablist<Key> tabs={TABS} value="schedule" onChange={vi.fn()} ariaLabel="Profile" labels="active" />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(3)

    // Every tab still has its name — `getByRole` resolves `sr-only` text too.
    expect(screen.getByRole('tab', { name: 'Profile' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Schedules' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Goals/ })).toBeInTheDocument()

    // Only the selected one lays its word out.
    const selected = screen.getByRole('tab', { name: 'Schedules' })
    expect(selected).toHaveAttribute('aria-selected', 'true')
    expect(visibleLabel(selected)).toHaveTextContent('Schedules')
    const profile = screen.getByRole('tab', { name: 'Profile' })
    expect(profile.querySelector('.sr-only')).toHaveTextContent('Profile')
    // The label span is sr-only; a count badge is not a label, so `Goals` can only be
    // read back from its hidden span.
    const goals = screen.getByRole('tab', { name: /Goals/ })
    expect(goals.querySelector('.sr-only')).toHaveTextContent('Goals')
    expect(goals.querySelector('.whitespace-nowrap')).toBeNull()
  })

  it('keeps the tooltip on an icon-only tab, so a pointer still learns what it is', () => {
    render(<Tablist<Key> tabs={TABS} value="profile" onChange={vi.fn()} ariaLabel="Profile" labels="active" />)
    expect(screen.getByRole('tab', { name: 'Schedules' })).toHaveAttribute('title', 'Schedules')
    expect(screen.getByRole('tab', { name: 'Profile' })).toHaveAttribute('title', 'Profile')
  })

  it('renders every icon regardless of which tab is selected', () => {
    render(<Tablist<Key> tabs={TABS} value="goals" onChange={vi.fn()} ariaLabel="Profile" labels="active" />)
    expect(screen.getByTestId('icon-profile')).toBeInTheDocument()
    expect(screen.getByTestId('icon-schedule')).toBeInTheDocument()
    expect(screen.getByTestId('icon-goals')).toBeInTheDocument()
  })

  it('moves the word with the selection: the newly selected tab shows it, the old one hides it', () => {
    function Host() {
      const [value, setValue] = useState<Key>('profile')
      return <Tablist<Key> tabs={TABS} value={value} onChange={setValue} ariaLabel="Profile" labels="active" />
    }
    render(<Host />)
    expect(visibleLabel(screen.getByRole('tab', { name: 'Profile' }))).toHaveTextContent('Profile')
    expect(screen.getByRole('tab', { name: 'Schedules' }).querySelector('.sr-only')).toHaveTextContent('Schedules')

    fireEvent.click(screen.getByRole('tab', { name: 'Schedules' }))
    expect(screen.getByRole('tab', { name: 'Schedules' })).toHaveAttribute('aria-selected', 'true')
    expect(visibleLabel(screen.getByRole('tab', { name: 'Schedules' }))).toHaveTextContent('Schedules')
    expect(screen.getByRole('tab', { name: 'Profile' }).querySelector('.sr-only')).toHaveTextContent('Profile')
  })

  it('still shows a non-zero count on an icon-only tab — the badge is information, the word is not', () => {
    render(<Tablist<Key> tabs={TABS} value="profile" onChange={vi.fn()} ariaLabel="Profile" labels="active" />)
    const goals = screen.getByRole('tab', { name: /Goals/ })
    expect(goals).toHaveTextContent('2')
  })

  it('default `always` lays out every label, exactly as before the prop existed', () => {
    render(<Tablist<Key> tabs={TABS} value="profile" onChange={vi.fn()} ariaLabel="Profile" />)
    for (const name of ['Profile', 'Schedules']) {
      const tab = screen.getByRole('tab', { name })
      expect(visibleLabel(tab)).toHaveTextContent(name)
      expect(tab.querySelector('.sr-only')).toBeNull()
    }
    expect(screen.getByRole('tab', { name: /Goals/ }).querySelector('.sr-only')).toBeNull()
  })

  it('keeps the keyboard model under `active`: arrows move the selection off an icon-only tab', () => {
    const onChange = vi.fn()
    render(<Tablist<Key> tabs={TABS} value="profile" onChange={onChange} ariaLabel="Profile" labels="active" />)
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Profile' }), { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith('schedule')
  })
})
