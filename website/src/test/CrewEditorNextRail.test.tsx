/**
 * Guards on the crewmate editor's section rail (`CrewEditorNextRail`).
 *
 * It is a find_ui segment host: a segment's marker must land on that
 * section's tab and on no other row, or a guide that opens the section on the
 * way to a control inside it clicks the wrong thing. The lock is pinned too:
 * while a draft that unmounting would lose is live, only the active section
 * stays enabled.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import CrewEditorNextRail from '../components/crew/CrewEditorNextRail'

type Key = 'general' | 'memory' | 'danger'
const SECTIONS: { key: Key; label: string }[] = [
  { key: 'general', label: 'General' },
  { key: 'memory', label: 'Memory' },
  { key: 'danger', label: 'Danger Zone' },
]

describe('mate editor rail', () => {
  it("puts a segment's marker on that section's tab, and on no other row", () => {
    render(
      <CrewEditorNextRail<Key>
        sections={SECTIONS}
        value="general"
        onSelect={() => {}}
        locked={false}
        ariaLabel="Sections"
        dangerKey="danger"
        segments={[{ key: 'danger', label: 'Danger Zone', 'data-ui-location': 'agents.section-danger' }]}
      />,
    )
    const marked = document.querySelectorAll('[data-ui-location]')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toBe(screen.getByRole('tab', { name: 'Danger Zone' }))
  })

  it('selects a section, and while locked keeps only the active one enabled', () => {
    const onSelect = vi.fn()
    const { rerender } = render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={onSelect} locked={false} ariaLabel="Sections" />,
    )
    fireEvent.click(screen.getByRole('tab', { name: 'Memory' }))
    expect(onSelect).toHaveBeenCalledWith('memory')
    expect(screen.getByRole('tab', { name: 'General' })).toHaveAttribute('aria-selected', 'true')

    onSelect.mockClear()
    rerender(<CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={onSelect} locked ariaLabel="Sections" />)
    expect(screen.getByRole('tab', { name: 'General' })).toBeEnabled()
    expect(screen.getByRole('tab', { name: 'Memory' })).toBeDisabled()
    fireEvent.click(screen.getByRole('tab', { name: 'Memory' }))
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('mate editor rail — the mobile select mirrors the rail', () => {
  // Below `sm` the rail collapses to a SimpleSelect; both are always in the DOM
  // (CSS swaps them by breakpoint). SimpleSelect draws the Radix popup on a
  // pointer device and a native <select> on a touch one. These tests force the
  // touch branch — the phone path this change exists for — so the assertions
  // read the real <select> the user gets; the Radix popup itself is covered by
  // SimpleSelect's own tests and need not be re-driven here.
  beforeEach(() => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('coarse') || query.includes('hover: none'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  // The native <select> on the touch path carries the rail's ariaLabel and is
  // the only combobox in the tree (the expanded rail's rows are tabs).
  const picker = () => screen.getByRole('combobox', { name: 'Sections' }) as HTMLSelectElement

  it('renders one option per section, in order, carrying each label', () => {
    render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={() => {}} locked={false} ariaLabel="Sections" />,
    )
    expect(Array.from(picker().options).map(o => o.textContent)).toEqual(['General', 'Memory', 'Danger Zone'])
  })

  it('reflects the active section as the select value', () => {
    render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="memory" onSelect={() => {}} locked={false} ariaLabel="Sections" />,
    )
    expect(picker().value).toBe('memory')
  })

  it('calls onSelect with the picked section', () => {
    const onSelect = vi.fn()
    render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={onSelect} locked={false} ariaLabel="Sections" />,
    )
    fireEvent.change(picker(), { target: { value: 'danger' } })
    expect(onSelect).toHaveBeenCalledWith('danger')
  })

  it('disables the whole select while locked, and refuses a change', () => {
    const onSelect = vi.fn()
    render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={onSelect} locked ariaLabel="Sections" />,
    )
    expect(picker()).toBeDisabled()
    fireEvent.change(picker(), { target: { value: 'memory' } })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('shows the locked hint only while locked, so the greyed select is not mute', () => {
    const { rerender } = render(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={() => {}} locked={false} ariaLabel="Sections" lockedHint="Save or discard your draft to switch sections." />,
    )
    expect(screen.queryByTestId('next-rail-locked-hint')).not.toBeInTheDocument()
    rerender(
      <CrewEditorNextRail<Key> sections={SECTIONS} value="general" onSelect={() => {}} locked ariaLabel="Sections" lockedHint="Save or discard your draft to switch sections." />,
    )
    expect(screen.getByTestId('next-rail-locked-hint')).toHaveTextContent('Save or discard your draft to switch sections.')
  })
})
