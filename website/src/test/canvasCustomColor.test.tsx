/**
 * The custom colour picker as the canvas drives it: each colour it reports comes
 * back as its `hex`, so a drag through black or grey keeps the hue it was on,
 * while a colour from outside still moves the picker.
 */
import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import CustomColor from '../components/canvas/CustomColor'

/** Give the square a real size, as a browser lays it out. */
const sized = (el: HTMLElement) => {
  el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) })
  return el
}

/** Feeds every reported colour back as `hex`, as the shape toolbar does through the canvas. */
function Controlled({ initial, onHex }: { initial: string; onHex: (hex: string, final: boolean, fresh: boolean) => void }) {
  const [hex, setHex] = useState(initial)
  return <CustomColor hex={hex} start={initial} onHex={(h, final, fresh) => { setHex(h); onHex(h, final, fresh) }} />
}

describe('the custom colour picker fed its own colour back', () => {
  it('keeps the hue through black and grey during a drag of the square', () => {
    const onHex = vi.fn()
    render(<Controlled initial="#0000FF" onHex={onHex} />)
    const square = sized(screen.getByTestId('custom-square'))

    // Down to black, then back up to full colour, in one drag.
    fireEvent.pointerDown(square, { clientX: 100, clientY: 100, pointerId: 1 })
    expect(onHex).toHaveBeenLastCalledWith('#000000', false, true)
    fireEvent.pointerMove(square, { clientX: 100, clientY: 0, buttons: 1, pointerId: 1 })
    expect(onHex).toHaveBeenLastCalledWith('#0000FF', false, false)

    // Left to grey, then right again.
    fireEvent.pointerMove(square, { clientX: 0, clientY: 0, buttons: 1, pointerId: 1 })
    expect(onHex).toHaveBeenLastCalledWith('#FFFFFF', false, false)
    fireEvent.pointerUp(square, { clientX: 100, clientY: 0, pointerId: 1 })
    expect(onHex).toHaveBeenLastCalledWith('#0000FF', true, false)
  })

  it('ignores an eyedropper result after the picker unmounts', async () => {
    let resolveDropper!: (value: { sRGBHex: string }) => void
    const result = new Promise<{ sRGBHex: string }>(resolve => { resolveDropper = resolve })
    const open = vi.fn(() => result)
    vi.stubGlobal('EyeDropper', class { open = open })
    try {
      const onHex = vi.fn()
      const { unmount } = render(<CustomColor hex="#FF0000" start="#FF0000" onHex={onHex} />)
      fireEvent.click(screen.getByLabelText('Pick a colour from the screen'))
      expect(open).toHaveBeenCalledTimes(1)
      unmount()
      await act(async () => { resolveDropper({ sRGBHex: '#123456' }); await result })
      expect(onHex).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('still follows a colour from outside', () => {
    const onHex = vi.fn()
    const { rerender } = render(<CustomColor hex="#0000FF" start="#0000FF" onHex={onHex} />)
    rerender(<CustomColor hex="#00FF00" start="#0000FF" onHex={onHex} />)
    expect((screen.getByLabelText('Hex colour') as HTMLInputElement).value).toBe('#00FF00')
    const square = sized(screen.getByTestId('custom-square'))
    fireEvent.pointerDown(square, { clientX: 100, clientY: 50, pointerId: 1 })
    expect(onHex).toHaveBeenLastCalledWith('#008000', false, true)
  })
})
