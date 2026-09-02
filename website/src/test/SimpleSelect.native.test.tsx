import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SimpleSelect from '../components/SimpleSelect'

/**
 * SimpleSelect on a TOUCH device renders a native `<select>` rather than the
 * Radix popup, so the platform's own picker does the scrolling.
 *
 * WHY: the Radix popup's list is a `position:fixed` overflow scroller inside a
 * scroll lock, and on iOS Safari a finger drag does not reliably move it — a
 * 40-row list (the STT language codes) is unreachable past the first screenful.
 * A native list cannot have that problem.
 *
 * These tests pin the CONTRACT the branch has to keep, not the look: every
 * sentinel SimpleSelect owns (empty value, action row, a value matching no
 * option) has to behave the same on both paths, because callers cannot see
 * which one they got.
 */
function stubTouch(isTouch: boolean) {
  const original = window.matchMedia
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: isTouch && (query === '(pointer: coarse)' || query === '(hover: none)'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
  return () => Object.defineProperty(window, 'matchMedia', { writable: true, value: original })
}

let restore: (() => void) | null = null
beforeEach(() => { restore = stubTouch(true) })
afterEach(() => { restore?.(); restore = null })

const LANGS = ['en-US', 'en-GB', 'fr-FR', 'zh-CN']

describe('SimpleSelect on a touch device', () => {
  it('renders a native select carrying every option, not the Radix popup', () => {
    render(<SimpleSelect options={LANGS} value="en-GB" onChange={() => {}} aria-label="Language" />)
    const control = screen.getByRole('combobox', { name: 'Language' })
    expect(control.tagName).toBe('SELECT')
    // All four rows exist WITHOUT opening anything: the platform picker needs no
    // click to have its list, which is the whole point of the branch.
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(LANGS)
    expect((control as HTMLSelectElement).value).toBe('en-GB')
  })

  it('fires onChange with the picked value', () => {
    const onChange = vi.fn()
    render(<SimpleSelect options={LANGS} value="en-US" onChange={onChange} aria-label="Language" />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Language' }), { target: { value: 'zh-CN' } })
    expect(onChange).toHaveBeenCalledWith('zh-CN')
  })

  it('optionLabels drive the row text while the value stays the option', () => {
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['piper', 'polly']} optionLabels={['Piper (local)', 'Amazon Polly']}
        value="piper" onChange={onChange} aria-label="Provider" />
    )
    expect(screen.getByRole('option', { name: 'Amazon Polly' })).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: 'Provider' }), { target: { value: 'polly' } })
    expect(onChange).toHaveBeenCalledWith('polly')
  })

  it('clearLabel is a real row that clears the value to empty string', () => {
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['a', 'b']} value="a" onChange={onChange} clearLabel="— none —" aria-label="Copy from" />
    )
    const clear = screen.getByRole('option', { name: '— none —' }) as HTMLOptionElement
    fireEvent.change(screen.getByRole('combobox', { name: 'Copy from' }), { target: { value: clear.value } })
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('the action row fires onSelect and never reports a value change', () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <SimpleSelect options={['ws-a']} value="ws-a" onChange={onChange} aria-label="Workspace"
        action={{ label: '+ New workspace…', onSelect }} />
    )
    const row = screen.getByRole('option', { name: '+ New workspace…' }) as HTMLOptionElement
    fireEvent.change(screen.getByRole('combobox', { name: 'Workspace' }), { target: { value: row.value } })
    expect(onSelect).toHaveBeenCalledOnce()
    expect(onChange).not.toHaveBeenCalled()
  })

  /**
   * The failure this guards is silent and worse than a visual glitch: a native
   * select whose value matches no option displays the FIRST one, so a language
   * the provider stopped offering would read as "en-US" — a setting the user
   * never chose, one tap away from being saved.
   */
  it('a value matching no option shows the fallback, not the first option', () => {
    const onChange = vi.fn()
    const { container } = render(
      <SimpleSelect options={['en-US', 'en-GB']} value="zh-CN" onChange={onChange}
        triggerFallback="zh-CN" aria-label="Language" />
    )
    const control = screen.getByRole('combobox', { name: 'Language' }) as HTMLSelectElement
    // Queried by the `hidden` attribute rather than by role+name: a hidden option
    // is out of the accessibility tree, so it has no accessible name to match on.
    const shown = container.querySelector<HTMLOptionElement>('option[hidden]')!
    expect(shown.textContent).toBe('zh-CN')
    expect(control.value).toBe('zh-CN')
    // Hidden rather than disabled: a disabled option renders grey when displayed,
    // which reads as the control being disabled. Hidden also keeps the slot out of
    // the picker's list, so it offers only values that can actually be chosen.
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['en-US', 'en-GB'])
    // The row carries the CURRENT value, so an engine that ignores `hidden` and
    // lets it be picked reports the value already set — the setting cannot change
    // behind the user's back. That is what removes the need for a guard.
    fireEvent.change(control, { target: { value: shown.value } })
    for (const call of onChange.mock.calls) expect(call[0]).toBe('zh-CN')
  })

  it('keeps the Radix popup on a pointer device', () => {
    restore?.()
    restore = stubTouch(false)
    render(<SimpleSelect options={LANGS} value="en-GB" onChange={() => {}} aria-label="Language" />)
    expect(screen.getByRole('combobox', { name: 'Language' }).tagName).toBe('BUTTON')
  })
})
