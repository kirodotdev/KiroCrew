/**
 * SearchableSelect runs against REAL Radix Popover — no mock. Popover opens on
 * click in jsdom (like Select, unlike DropdownMenu), and integration/setup.ts
 * already polyfills scrollIntoView + the pointer-capture methods Radix's
 * DismissableLayer needs.
 *
 * The listbox rows are plain <button role="option">, so they are addressable
 * without a portal-aware query helper once the popup is open.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import SearchableSelect, { type SearchableSelectOption } from '../components/SearchableSelect'
import TimezoneSelect from '../components/TimezoneSelect'

const OPTS: SearchableSelectOption[] = [
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles', sublabel: 'UTC-8', keywords: 'America Los Angeles' },
  { value: 'Asia/Shanghai', label: 'Asia/Shanghai', sublabel: 'UTC+8', keywords: 'Asia Shanghai' },
  { value: 'Europe/Berlin', label: 'Europe/Berlin', sublabel: 'UTC+1', keywords: 'Europe Berlin' },
  { value: 'locked', label: 'Locked option', disabled: true },
]

function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Timezone' }))
  return screen.findByRole('listbox')
}

describe('SearchableSelect', () => {
  it('shows the selected label and its sublabel in the trigger', () => {
    render(<SearchableSelect options={OPTS} value="Asia/Shanghai" onChange={() => {}} aria-label="Timezone" />)
    expect(screen.getByRole('button', { name: 'Timezone' })).toHaveTextContent('Asia/Shanghai (UTC+8)')
  })

  it('falls back to triggerFallback when the value matches no option', () => {
    render(<SearchableSelect options={OPTS} value="Mars/Olympus" onChange={() => {}} triggerFallback="Pick one" aria-label="Timezone" />)
    expect(screen.getByRole('button', { name: 'Timezone' })).toHaveTextContent('Pick one')
  })

  it('opens a listbox and marks only the current value as selected', async () => {
    render(<SearchableSelect options={OPTS} value="Asia/Shanghai" onChange={() => {}} aria-label="Timezone" />)
    await open()
    const selected = screen.getAllByRole('option').filter(o => o.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveLength(1)
    expect(selected[0]).toHaveTextContent('Asia/Shanghai')
  })

  it('filters on label, offset and keywords, matching tokens in any order', async () => {
    render(<SearchableSelect options={OPTS} value="" onChange={() => {}} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')

    fireEvent.change(box, { target: { value: 'shang' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)

    // Reversed tokens still match — the filter ANDs every token.
    fireEvent.change(box, { target: { value: 'angeles america' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(screen.getByRole('option')).toHaveTextContent('America/Los_Angeles')

    // The sublabel is searchable too.
    fireEvent.change(box, { target: { value: 'utc+1' } })
    expect(screen.getByRole('option')).toHaveTextContent('Europe/Berlin')
  })

  it('renders an empty state when nothing matches', async () => {
    render(<SearchableSelect options={OPTS} value="" onChange={() => {}} aria-label="Timezone" />)
    await open()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'zzzz' } })
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText('No matches')).toBeInTheDocument()
  })

  it('commits a click and closes', async () => {
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    fireEvent.click(screen.getByRole('option', { name: /Europe\/Berlin/ }))
    expect(onChange).toHaveBeenCalledWith('Europe/Berlin')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('never commits a disabled option', async () => {
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    fireEvent.click(screen.getByRole('option', { name: /Locked option/ }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits the sole remaining match on Enter in the filter box', async () => {
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'berlin' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('Europe/Berlin')
  })

  it('commits the TOP match on Enter even while several options still match', async () => {
    // Combobox convention: Enter takes the row the user is looking at. Requiring
    // exactly one match left Enter silent mid-typing on a 420-item list, which is
    // what the UX review flagged.
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'a' } })
    expect(screen.getAllByRole('option').length).toBeGreaterThan(1)
    const first = screen.getAllByRole('option')[0].textContent
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(first).toContain(onChange.mock.calls[0][0])
  })

  it('does not commit on Enter when nothing matches', async () => {
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'zzzz' } })
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('ignores Enter that is confirming an IME composition', async () => {
    // A CJK input method confirms its candidate with Enter. If that reached the
    // listbox handler it would commit the top match and close the picker, eating
    // the text the user was composing.
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'berlin' } })
    fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
    expect(onChange).not.toHaveBeenCalled()
    // The same key, once composition has ended, still commits.
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('Europe/Berlin')
  })

  it('leaves a disabled option focusable so ArrowDown can move past it', async () => {
    // A `disabled` button cannot take focus, so useListboxKeyboard's .focus()
    // would no-op and the keyboard would stall in the filter box. aria-disabled
    // keeps the row reachable while still refusing to commit it.
    const onChange = vi.fn()
    render(<SearchableSelect options={OPTS} value="" onChange={onChange} aria-label="Timezone" />)
    await open()
    const locked = screen.getByRole('option', { name: /Locked option/ })
    expect(locked).toHaveAttribute('aria-disabled', 'true')
    expect(locked).not.toHaveAttribute('disabled')
    locked.focus()
    expect(document.activeElement).toBe(locked)
    fireEvent.click(locked)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('ArrowDown from the filter box moves focus onto the first option', async () => {
    render(<SearchableSelect options={OPTS} value="" onChange={() => {}} aria-label="Timezone" />)
    await open()
    const box = screen.getByRole('textbox')
    box.focus()
    fireEvent.keyDown(box, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(screen.getAllByRole('option')[0])
  })

  it('clears the filter between openings so the full list comes back', async () => {
    render(<SearchableSelect options={OPTS} value="" onChange={() => {}} aria-label="Timezone" />)
    await open()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'berlin' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    await open()
    expect(screen.getAllByRole('option')).toHaveLength(OPTS.length)
  })

  it('is not openable while disabled', () => {
    render(<SearchableSelect options={OPTS} value="" onChange={() => {}} disabled aria-label="Timezone" />)
    fireEvent.click(screen.getByRole('button', { name: 'Timezone' }))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})

describe('TimezoneSelect', () => {
  it('carries the id through so an external label can address the trigger', () => {
    render(<TimezoneSelect id="schedule-render-tz" value="UTC" onChange={() => {}} />)
    expect(screen.getByRole('button', { name: 'Render timezone' })).toHaveAttribute('id', 'schedule-render-tz')
  })

  it('shows the current zone with its offset, and keeps a value absent from the IANA list', () => {
    render(<TimezoneSelect value="Factory/Legacy" onChange={() => {}} />)
    // The unknown zone is prepended rather than dropped, so it stays visible.
    expect(screen.getByRole('button', { name: 'Render timezone' })).toHaveTextContent('Factory/Legacy')
  })

  it('does not render "UTC (UTC)" — a sublabel that repeats the zone is dropped', () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    expect(screen.getByRole('button', { name: 'Render timezone' })).toHaveTextContent(/^UTC$/)
  })

  it('is searchable by city name without the underscore', async () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="UTC" onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Render timezone' }))
    await screen.findByRole('listbox')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'los angeles' } })
    const opts = screen.getAllByRole('option')
    expect(opts).toHaveLength(1)
    fireEvent.click(opts[0])
    expect(onChange).toHaveBeenCalledWith('America/Los_Angeles')
  })
})
