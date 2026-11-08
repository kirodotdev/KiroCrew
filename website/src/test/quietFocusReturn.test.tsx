/**
 * A pointer open → pick → close of a menu or select hands focus back to its
 * trigger WITHOUT the keyboard ring; a keyboard close keeps it. A filter-less
 * listbox opened by pointer focuses its option without the ring too.
 *
 * Radix's DropdownMenu and Select open on pointerdown and cancel it, so the
 * trigger never takes mouse focus, and Chromium paints `:focus-visible` on the
 * scripted return. jsdom does not compute `:focus-visible`, so these tests pin
 * the contract one level down: the `focus()` call that returns focus to the
 * trigger carries `focusVisible: false` after pointer input, and does not
 * after keyboard input.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRef } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { lastInputIsPointer, quietNextFocus } from '../lib/quietFocusReturn'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/** Every `focus()` options object passed for *el* since the spy was installed. */
function focusCallsOn(spy: ReturnType<typeof vi.spyOn>, el: HTMLElement) {
  return spy.mock.contexts
    .map((ctx, i) => [ctx, spy.mock.calls[i][0]] as const)
    .filter(([ctx]) => ctx === el)
    .map(([, opts]) => opts as FocusOptions | undefined)
}

describe('quietNextFocus', () => {
  it('after pointer input, the next focus() skips the ring and the shim is gone', () => {
    const el = document.createElement('button')
    document.body.appendChild(el)
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    fireEvent.pointerDown(document.body)
    expect(lastInputIsPointer()).toBe(true)

    quietNextFocus(el)
    el.focus({ preventScroll: true })

    expect(focusCallsOn(spy, el)).toEqual([{ preventScroll: true, focusVisible: false }])
    expect(Object.prototype.hasOwnProperty.call(el, 'focus')).toBe(false)
    el.remove()
  })

  it('after keyboard input it changes nothing', () => {
    const el = document.createElement('button')
    document.body.appendChild(el)
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(lastInputIsPointer()).toBe(false)

    quietNextFocus(el)
    expect(Object.prototype.hasOwnProperty.call(el, 'focus')).toBe(false)
    el.remove()
  })

  it('a bare modifier key does not count as keyboard input (Shift+click)', () => {
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'Shift' })
    expect(lastInputIsPointer()).toBe(true)
  })

  it('an unused shim is dropped at the end of the task', () => {
    vi.useFakeTimers()
    const el = document.createElement('button')
    fireEvent.pointerDown(document.body)
    quietNextFocus(el)
    expect(Object.prototype.hasOwnProperty.call(el, 'focus')).toBe(true)
    vi.runAllTimers()
    expect(Object.prototype.hasOwnProperty.call(el, 'focus')).toBe(false)
  })
})

function Menu() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild><button>Open</button></DropdownMenuTrigger>
      <DropdownMenuContent><DropdownMenuItem>Pick</DropdownMenuItem></DropdownMenuContent>
    </DropdownMenu>
  )
}

describe('DropdownMenu focus return', () => {
  it('a pointer pick returns focus to the trigger without the ring', async () => {
    const user = userEvent.setup()
    render(<Menu />)
    const trigger = screen.getByRole('button', { name: 'Open' })
    await user.click(trigger)
    const item = await screen.findByRole('menuitem', { name: 'Pick' })
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    await user.click(item)

    await waitFor(() => expect(trigger).toHaveFocus())
    expect(focusCallsOn(spy, trigger)).toEqual([expect.objectContaining({ focusVisible: false })])
  })

  it('a keyboard close keeps the ring', async () => {
    const user = userEvent.setup()
    render(<Menu />)
    const trigger = screen.getByRole('button', { name: 'Open' })
    trigger.focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('menuitem', { name: 'Pick' })
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    await user.keyboard('{Escape}')

    await waitFor(() => expect(trigger).toHaveFocus())
    const calls = focusCallsOn(spy, trigger)
    expect(calls.length).toBeGreaterThan(0)
    for (const opts of calls) expect(opts?.focusVisible).not.toBe(false)
  })
})

/** A filter-less listbox on useListboxKeyboard, which focuses its selected option on open. */
function Listbox({ open }: { open: boolean }) {
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown } = useListboxKeyboard({
    open, dropdownRef, inputRef, hasFilterInput: false, filteredCount: 2,
    onEnterSingleMatch: () => {}, closeToTrigger: () => {},
  })
  if (!open) return null
  return (
    <div ref={dropdownRef} role="listbox" aria-label="Letters" tabIndex={-1} onKeyDown={onListKeyDown}>
      <button role="option" aria-selected={false} tabIndex={-1}>A</button>
      <button role="option" aria-selected tabIndex={-1}>B</button>
    </div>
  )
}

describe('useListboxKeyboard open focus', () => {
  it('a pointer open focuses the selected option without the ring', async () => {
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    const { rerender } = render(<Listbox open={false} />)
    fireEvent.pointerDown(document.body)
    rerender(<Listbox open />)
    const option = screen.getByRole('option', { name: 'B' })

    await waitFor(() => expect(option).toHaveFocus())
    expect(focusCallsOn(spy, option)).toEqual([{ focusVisible: false }])
  })

  it('a keyboard open keeps the ring', async () => {
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    const { rerender } = render(<Listbox open={false} />)
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'Enter' })
    rerender(<Listbox open />)
    const option = screen.getByRole('option', { name: 'B' })

    await waitFor(() => expect(option).toHaveFocus())
    const calls = focusCallsOn(spy, option)
    expect(calls.length).toBe(1)
    expect(calls[0]?.focusVisible).not.toBe(false)
  })
})

describe('Select focus return', () => {
  it('a pointer pick returns focus to the trigger without the ring', async () => {
    const user = userEvent.setup()
    render(
      <Select defaultValue="a">
        <SelectTrigger aria-label="Letter"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="a">A</SelectItem>
          <SelectItem value="b">B</SelectItem>
        </SelectContent>
      </Select>,
    )
    const trigger = screen.getByRole('combobox', { name: 'Letter' })
    await user.click(trigger)
    const option = await screen.findByRole('option', { name: 'B' })
    const spy = vi.spyOn(HTMLElement.prototype, 'focus')
    await user.click(option)

    await waitFor(() => expect(trigger).toHaveFocus())
    expect(focusCallsOn(spy, trigger)).toEqual([expect.objectContaining({ focusVisible: false })])
  })
})
