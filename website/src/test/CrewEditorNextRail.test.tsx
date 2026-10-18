/**
 * Guards on the crewmate editor's section rail (`CrewEditorNextRail`).
 *
 * It is a find_ui segment host: a segment's marker must land on that
 * section's tab and on no other row, or a guide that opens the section on the
 * way to a control inside it clicks the wrong thing. The lock is pinned too:
 * while a draft that unmounting would lose is live, only the active section
 * stays enabled.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import CrewEditorNextRail from '../components/crew/CrewEditorNextRail'

type Key = 'general' | 'memory' | 'danger'
const SECTIONS: { key: Key; label: string }[] = [
  { key: 'general', label: 'General' },
  { key: 'memory', label: 'Memory' },
  { key: 'danger', label: 'Danger Zone' },
]

describe('crewmate editor rail', () => {
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
