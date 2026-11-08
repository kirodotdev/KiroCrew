/**
 * Two armed traps at once: the newest MOUNTED one owns Tab. Without that, each
 * trap pulls focus back inside itself on every Tab and focus can never leave
 * the newer dialog's first control.
 */
import { describe, it, expect } from 'vitest'
import { useRef, useState } from 'react'
import { render, fireEvent, act } from '@testing-library/react'
import { FOCUSABLE, useDialogFocusTrap } from './useDialogFocusTrap'

function Dialog({ name }: { name: string }) {
  const ref = useRef<HTMLDivElement>(null)
  // An inline onEscape re-arms the trap on every render, as many callers do.
  useDialogFocusTrap(ref, () => {})
  return (
    <div ref={ref} role="dialog" aria-label={name}>
      <button type="button">{name} one</button>
      <button type="button">{name} two</button>
    </div>
  )
}

/** jsdom lays nothing out; give every focusable a parent so the trap sees it. */
function markVisible() {
  for (const el of document.body.querySelectorAll<HTMLElement>(FOCUSABLE)) {
    Object.defineProperty(el, 'offsetParent', { configurable: true, get: () => document.body })
  }
}

let rerenderOuter: () => void = () => {}
let setInner: (v: boolean) => void = () => {}
function Host() {
  const [, setTick] = useState(0)
  const [inner, setInnerState] = useState(true)
  rerenderOuter = () => setTick(t => t + 1)
  setInner = setInnerState
  return (
    <>
      <Dialog name="Outer" />
      {inner && <Dialog name="Inner" />}
    </>
  )
}

describe('useDialogFocusTrap: stacked traps', () => {
  it('lets Tab move inside the newer dialog while an older one is armed', () => {
    const { getByText } = render(<Host />)
    markVisible()
    const innerOne = getByText('Inner one')
    innerOne.focus()
    // `false` would mean a trap claimed the key and moved focus itself; a
    // mid-dialog Tab is the browser's to move.
    expect(fireEvent.keyDown(innerOne, { key: 'Tab' })).toBe(true)
    expect(document.activeElement).toBe(innerOne)
  })

  it('keeps the newer dialog in front after the older one re-renders', () => {
    const { getByText } = render(<Host />)
    act(() => { rerenderOuter() })
    markVisible()
    const innerOne = getByText('Inner one')
    innerOne.focus()
    expect(fireEvent.keyDown(innerOne, { key: 'Tab' })).toBe(true)
    const innerTwo = getByText('Inner two')
    innerTwo.focus()
    fireEvent.keyDown(innerTwo, { key: 'Tab' })
    expect(document.activeElement).toBe(innerOne)
  })

  it('hands Tab back to the older dialog once the newer one closes', () => {
    const { getByText } = render(<Host />)
    act(() => { setInner(false) })
    markVisible()
    const outerTwo = getByText('Outer two')
    outerTwo.focus()
    fireEvent.keyDown(outerTwo, { key: 'Tab' })
    expect(document.activeElement).toBe(getByText('Outer one'))
  })
})
