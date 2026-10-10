import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'

import ChatInput from '../components/ChatInput'
import MicSourceMenu from '../components/MicSourceMenu'
import { ContextUsageControl, useContextPopover } from '../components/chat-input/ContextShelf'
import { renderWithProviders } from './helpers'

// The follow-up to #17536: the hand-rolled composer popovers (project chip,
// microphone source menu, context-usage readout) open on the mouse PRESS like
// the model and agent chips, and keyboard / touch still act on click.

/** A real browser's plain left-button sequence: pointerdown, mousedown, then
 *  the click on release (detail 1). */
const press = (el: Element) => {
  act(() => { el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 })) })
  act(() => { el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 })) })
}
const release = (el: Element) => {
  act(() => { el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0 })) })
  act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, detail: 1 })) })
}
const touchTap = (el: Element) => {
  act(() => { el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', button: 0 })) })
  act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true, button: 0, detail: 1 })) })
}

/** Let async effects (theme load, device enumeration) settle inside act. */
const flush = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })

/** The same press as `press`, reporting whether the `mousedown` default was
 *  cancelled. jsdom never moves focus on mousedown, so the cancel is what a
 *  test can observe of "the trigger keeps focus off itself". */
const pressCancelsFocus = (el: Element, init: MouseEventInit = {}) => {
  let notCancelled = true
  act(() => { el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0, ...init })) })
  act(() => { notCancelled = el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, ...init })) })
  return !notCancelled
}

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { enumerateDevices: vi.fn().mockResolvedValue([]) },
  })
})
afterEach(() => vi.restoreAllMocks())

describe('project chip', () => {
  const chip = () => screen.getByRole('button', { name: /Project: |Select project/ })

  it('fires onProjectClick on the press and not again on the release', async () => {
    const onProjectClick = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onProjectClick={onProjectClick} project="/home/u/work/KiroCrew" />)
    await flush()
    press(chip())
    expect(onProjectClick).toHaveBeenCalledTimes(1)
    expect(onProjectClick.mock.calls[0][1]).toBe(chip())
    release(chip())
    expect(onProjectClick).toHaveBeenCalledTimes(1)
  })

  it('still fires on a keyboard click and on a touch tap', async () => {
    const onProjectClick = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onProjectClick={onProjectClick} project="/home/u/work/KiroCrew" />)
    await flush()
    fireEvent.click(chip())
    expect(onProjectClick).toHaveBeenCalledTimes(1)
    touchTap(chip())
    expect(onProjectClick).toHaveBeenCalledTimes(2)
  })

  it('does nothing on a press while a response is running', async () => {
    const onProjectClick = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onProjectClick={onProjectClick} project="/home/u/work/KiroCrew" isRunning onStop={vi.fn()} />)
    await flush()
    const btn = screen.getByRole('button', { name: /Stop the current response to switch project/ })
    press(btn)
    release(btn)
    expect(onProjectClick).not.toHaveBeenCalled()
  })

  it('keeps focus off the chip when the press moved focus into the picker, and not after a close', async () => {
    const search = document.createElement('input')
    let open = false
    const onProjectClick = vi.fn(() => {
      open = !open
      if (open) { document.body.appendChild(search); search.focus() } else search.remove()
    })
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onProjectClick={onProjectClick} project="/home/u/work/KiroCrew" />)
    await flush()
    expect(pressCancelsFocus(chip())).toBe(true)
    expect(document.activeElement).toBe(search)
    release(chip())
    expect(pressCancelsFocus(chip())).toBe(false)
    release(chip())
    expect(pressCancelsFocus(chip(), { shiftKey: true })).toBe(false)
  })
})

describe('MicSourceMenu trigger', () => {
  const trigger = () => screen.getByRole('button', { name: /input source/i })

  it('opens on the press, stays open through the release, and a second press closes it', async () => {
    render(<MicSourceMenu onSelect={() => {}} />)
    await flush()
    press(trigger())
    await flush()
    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
    release(trigger())
    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
    press(trigger())
    release(trigger())
    expect(trigger()).toHaveAttribute('aria-expanded', 'false')
  })

  it('still opens on a keyboard click', async () => {
    render(<MicSourceMenu onSelect={() => {}} />)
    await flush()
    fireEvent.click(trigger())
    await flush()
    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
  })

  it('leaves focus on the first menu item after a press opens it, and lets the trigger take it on the closing press', async () => {
    render(<MicSourceMenu onSelect={() => {}} />)
    await flush()
    expect(pressCancelsFocus(trigger())).toBe(true)
    await flush()
    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
    expect(document.activeElement).toBe(screen.getAllByRole('menuitemradio')[0])

    release(trigger())
    expect(pressCancelsFocus(trigger())).toBe(false)
    expect(trigger()).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('context-usage readout', () => {
  const autoCompactThreshold = {
    autoCompactQuery: { isLoading: false, isError: false },
    autoCompact: null,
    autoCompactError: '',
    setAutoCompactError: () => {},
    pushAutoCompact: () => {},
  } as unknown as Parameters<typeof ContextUsageControl>[0]['autoCompactThreshold']

  function Host() {
    const { ctxPopoverOpen, setCtxPopoverOpen, ctxWrapRef } = useContextPopover()
    return (
      <>
        <ContextUsageControl contextPct={40} contextWindowTokens={1000} shelfCompact={false}
          ctxPopoverOpen={ctxPopoverOpen} setCtxPopoverOpen={setCtxPopoverOpen} ctxWrapRef={ctxWrapRef}
          autoCompactThreshold={autoCompactThreshold} />
        <button type="button">outside</button>
      </>
    )
  }
  const readout = () => screen.getByRole('button', { name: /context usage/i })
  const popoverShown = () => screen.queryByText(/Context window/i) !== null

  it('opens on the press, stays open through the release, and a second press closes it', async () => {
    render(<Host />)
    await flush()
    press(readout())
    expect(popoverShown()).toBe(true)
    release(readout())
    expect(popoverShown()).toBe(true)
    press(readout())
    release(readout())
    expect(popoverShown()).toBe(false)
  })

  it('still opens on a keyboard click, and a press outside closes it', async () => {
    render(<Host />)
    await flush()
    fireEvent.click(readout())
    expect(popoverShown()).toBe(true)
    press(screen.getByRole('button', { name: 'outside' }))
    expect(popoverShown()).toBe(false)
  })
})
