import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { isConsumedPressClick, usePressActivation } from '../hooks/usePressActivation'

function Harness({ onPress, disabled }: { onPress: () => void; disabled?: boolean }) {
  const bind = usePressActivation()
  return <button disabled={disabled} {...bind(() => onPress())}>go</button>
}

function setup(disabled?: boolean) {
  const onPress = vi.fn()
  render(<Harness onPress={onPress} disabled={disabled} />)
  return { onPress, button: screen.getByRole('button', { name: 'go' }) }
}

const mouseDown = (el: Element, init: PointerEventInit = {}) =>
  act(() => { el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'mouse', button: 0, ...init })) })

describe('usePressActivation', () => {
  it('acts on a mouse press and swallows the click that follows', () => {
    const { onPress, button } = setup()
    mouseDown(button)
    expect(onPress).toHaveBeenCalledTimes(1)
    let clickEvent: Event | null = null
    button.addEventListener('click', e => { clickEvent = e })
    fireEvent.click(button, { detail: 1 })
    expect(onPress).toHaveBeenCalledTimes(1)
    expect(clickEvent && isConsumedPressClick(clickEvent)).toBe(true)
  })

  it('acts exactly once for a real user click', async () => {
    const { onPress, button } = setup()
    const user = userEvent.setup()
    await user.pointer({ keys: '[MouseLeft>]', target: button })
    expect(onPress).toHaveBeenCalledTimes(1)
    await user.pointer({ keys: '[/MouseLeft]', target: button })
    expect(onPress).toHaveBeenCalledTimes(1)
  })

  it('acts on click for the keyboard', async () => {
    const { onPress, button } = setup()
    button.focus()
    await userEvent.keyboard('{Enter}')
    await userEvent.keyboard(' ')
    expect(onPress).toHaveBeenCalledTimes(2)
  })

  it('acts on click for touch, so a scroll that starts on the control does nothing', () => {
    const { onPress, button } = setup()
    act(() => { button.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', button: 0 })) })
    expect(onPress).not.toHaveBeenCalled()
    fireEvent.click(button, { detail: 1 })
    expect(onPress).toHaveBeenCalledTimes(1)
  })

  it('leaves right-button and modified presses to the click path', () => {
    const { onPress, button } = setup()
    mouseDown(button, { button: 2 })
    mouseDown(button, { ctrlKey: true })
    mouseDown(button, { metaKey: true })
    expect(onPress).not.toHaveBeenCalled()
    fireEvent.click(button, { detail: 1 })
    expect(onPress).toHaveBeenCalledTimes(1)
  })

  it('does not act on a press of a disabled control', () => {
    const { onPress, button } = setup(true)
    mouseDown(button)
    expect(onPress).not.toHaveBeenCalled()
  })

  it('does not swallow a keyboard click after a press that was dragged off', () => {
    // Press, drag away, release elsewhere: no click reaches the control. The
    // next Enter must still activate it.
    const { onPress, button } = setup()
    mouseDown(button)
    fireEvent.click(button, { detail: 0 })
    expect(onPress).toHaveBeenCalledTimes(2)
  })
})
