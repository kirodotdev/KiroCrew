import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import SimpleSelect from '../components/SimpleSelect'
import * as touch from '../utils/isTouchDevice'

/**
 * SimpleSelect — the shared Radix Select wrapper that replaced StyledSelect
 * (PublishHub, ArtifactDeployPage, KiroCrewAgentsPage) and backs SettingsSelect.
 * Runs against REAL @radix-ui/react-select: Radix Select opens on click in
 * jsdom (unlike DropdownMenu), so no mock is needed here.
 */
describe('SimpleSelect', () => {
  afterEach(() => vi.restoreAllMocks())

  it('on the native path keeps focus on the same element when busy flips (no remount)', () => {
    // The native <select> is the one control a keyboard user has focused; if
    // turning busy on changed the element tree, React would unmount and remount
    // it and focus would fall to the page -- the exact drop this fix prevents.
    vi.spyOn(touch, 'isTouchDevice').mockReturnValue(true)
    const onChange = vi.fn()
    const { rerender } = render(
      <SimpleSelect options={['a', 'b']} value="a" onChange={onChange} aria-label="Provider" />
    )
    const select = document.querySelector('select') as HTMLSelectElement
    expect(select).toBeTruthy()
    select.focus()
    expect(document.activeElement).toBe(select)
    rerender(<SimpleSelect options={['a', 'b']} value="a" onChange={onChange} busy aria-label="Provider" />)
    // Same element instance still mounted and still focused.
    expect(document.querySelector('select')).toBe(select)
    expect(document.activeElement).toBe(select)
    expect(select).toHaveAttribute('aria-disabled', 'true')
    expect(select).toHaveAttribute('aria-busy', 'true')
    // A change while busy is refused.
    fireEvent.change(select, { target: { value: 'b' } })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('keeps identity icons in the selected value and options without changing their text names', async () => {
    const onChange = vi.fn()
    render(<SimpleSelect options={['reviewer', 'writer']} optionLabels={['Code review', 'Writing']} optionIcons={[<img key="reviewer" src="/reviewer.png" alt="" />, <img key="writer" src="/writer.png" alt="" />]} value="reviewer" onChange={onChange} aria-label="Member" />)
    const trigger = screen.getByRole('combobox', { name: 'Member' })
    expect(trigger.querySelector('img')).toHaveAttribute('src', '/reviewer.png')
    fireEvent.click(trigger)
    const writer = await screen.findByRole('option', { name: 'Writing' })
    expect(writer.querySelector('img')).toHaveAttribute('src', '/writer.png')
    expect(within(writer).queryByRole('img')).toBeNull()
    fireEvent.click(writer)
    expect(onChange).toHaveBeenCalledWith('writer')
  })

  it('fires onChange with the selected value and shows it in the trigger', async () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <SimpleSelect options={['static', 'fullstack']} value="static" onChange={onChange} aria-label="Policy tier" />
    )
    const trigger = screen.getByRole('combobox', { name: 'Policy tier' })
    expect(trigger).toHaveTextContent('static')
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'fullstack' }))
    expect(onChange).toHaveBeenCalledWith('fullstack')
    rerender(<SimpleSelect options={['static', 'fullstack']} value="fullstack" onChange={onChange} aria-label="Policy tier" />)
    expect(trigger).toHaveTextContent('fullstack')
  })

  it('clearLabel renders as a selectable top row that clears to empty string', async () => {
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['a', 'b']} value="a" onChange={onChange} clearLabel="— none —" aria-label="Copy from" />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Copy from' }))
    fireEvent.click(await screen.findByRole('option', { name: '— none —' }))
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('shows clearLabel in the trigger while value is empty', () => {
    render(
      <SimpleSelect options={['a', 'b']} value="" onChange={() => {}} clearLabel="— none —" aria-label="Copy from" />
    )
    expect(screen.getByRole('combobox', { name: 'Copy from' })).toHaveTextContent('— none —')
  })

  it('Escape closes only the select, not a window-level Escape host (modal)', async () => {
    // Regression: Radix dismisses from a document listener, so without
    // stopPropagation in ui/select the same keydown reached the workspace
    // modal's window handler and closed the modal along with the dropdown.
    const onWindowEscape = vi.fn()
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onWindowEscape() }
    window.addEventListener('keydown', handler)
    try {
      render(<SimpleSelect options={['a', 'b']} value="a" onChange={() => {}} aria-label="Workspace" />)
      fireEvent.click(screen.getByRole('combobox', { name: 'Workspace' }))
      expect(await screen.findByRole('option', { name: 'b' })).toBeInTheDocument()
      fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
      expect(onWindowEscape).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', handler)
    }
  })

  it('action row fires onSelect instead of onChange', async () => {    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <SimpleSelect
        options={['default']} value="default" onChange={onChange}
        action={{ label: '+ New workspace…', onSelect }} aria-label="Workspace"
      />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Workspace' }))
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
  })
})
