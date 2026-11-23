import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import TerminalTouchHandles from '../components/TerminalTouchHandles'
import { TOUCH_HANDLE_ATTR } from '../hooks/useTerminalTouchSelection'
import { i18nT } from '../i18n/t'

/** The page-x span a handle's hit area covers: caret left + its own offset. */
function hitSpan(container: HTMLElement, which: 'start' | 'end'): [number, number] {
  const caret = container.querySelector<HTMLElement>(`[data-testid="terminal-touch-handle-${which}"]`)!
  const hit = caret.querySelector<HTMLElement>(`[${TOUCH_HANDLE_ATTR}]`)!
  const left = parseFloat(caret.style.left) + parseFloat(hit.style.left)
  return [left, left + parseFloat(hit.style.width)]
}

/** The page-y span a handle's hit area covers. */
function hitVSpan(container: HTMLElement, which: 'start' | 'end'): [number, number] {
  const caret = container.querySelector<HTMLElement>(`[data-testid="terminal-touch-handle-${which}"]`)!
  const hit = caret.querySelector<HTMLElement>(`[${TOUCH_HANDLE_ATTR}]`)!
  const top = parseFloat(caret.style.top) + parseFloat(hit.style.top)
  return [top, top + parseFloat(hit.style.height)]
}
const inside = (v: number, [a, b]: [number, number]) => v >= a && v < b

describe('TerminalTouchHandles', () => {
  it('splits overlapping hit areas when both knobs sit on the same side of one row', () => {
    // A two-character word at column 0 of the first visible row: the start knob
    // flips below, beside the end knob 16px away.
    const { container } = render(<TerminalTouchHandles handles={{
      start: { left: 0, top: 0, height: 20, flip: true },
      end: { left: 16, top: 0, height: 20 },
    }} />)
    const [, startRight] = hitSpan(container, 'start')
    const [endLeft] = hitSpan(container, 'end')
    expect(startRight).toBeLessThanOrEqual(endLeft)
    expect(startRight).toBeGreaterThan(0)
  })

  it('announces a touch selection through a live region that stays mounted', () => {
    const { container, rerender } = render(<TerminalTouchHandles handles={null} />)
    const region = container.querySelector('[role="status"][aria-live="polite"]')
    expect(region).not.toBeNull()
    expect(region!.textContent).toBe('')
    rerender(<TerminalTouchHandles handles={{
      start: { left: 40, top: 20, height: 20 },
      end: { left: 56, top: 20, height: 20 },
    }} />)
    // The same node, so a screen reader that saw it empty hears the change.
    expect(container.querySelector('[role="status"]')).toBe(region)
    expect(region!.textContent).toBe(i18nT('components.cliPanel.touch_select_done'))
    rerender(<TerminalTouchHandles handles={null} />)
    expect(region!.textContent).toBe('')
  })

  it('keeps each knob in its own hit area when the knobs sit on adjacent rows', () => {
    // 16px rows, same column. The start on the first visible row flips its knob
    // below the caret, right on top of the end knob's area one row down.
    const top = render(<TerminalTouchHandles handles={{
      start: { left: 40, top: 0, height: 16, flip: true },
      end: { left: 40, top: 16, height: 16 },
    }} />)
    // Knob centres: start 0 + 16 + 6, end 16 + 16 + 6.
    expect(inside(22, hitVSpan(top.container, 'start'))).toBe(true)
    expect(inside(22, hitVSpan(top.container, 'end'))).toBe(false)
    expect(inside(38, hitVSpan(top.container, 'end'))).toBe(true)
    expect(inside(38, hitVSpan(top.container, 'start'))).toBe(false)
    top.unmount()
    // The mirror case on the last two visible rows: the end flips its knob above.
    const bottom = render(<TerminalTouchHandles handles={{
      start: { left: 40, top: 32, height: 16 },
      end: { left: 40, top: 48, height: 16, flip: true },
    }} />)
    // Knob centres: start 32 - 12 + 6, end 48 - 12 + 6.
    expect(inside(26, hitVSpan(bottom.container, 'start'))).toBe(true)
    expect(inside(26, hitVSpan(bottom.container, 'end'))).toBe(false)
    expect(inside(42, hitVSpan(bottom.container, 'end'))).toBe(true)
    expect(inside(42, hitVSpan(bottom.container, 'start'))).toBe(false)
  })

  it('keeps full hit areas when the knobs sit on opposite sides', () => {
    const { container } = render(<TerminalTouchHandles handles={{
      start: { left: 40, top: 20, height: 20 },
      end: { left: 56, top: 20, height: 20 },
    }} />)
    const [sl, sr] = hitSpan(container, 'start')
    const [el, er] = hitSpan(container, 'end')
    expect(sr - sl).toBe(36)
    expect(er - el).toBe(36)
  })
})
