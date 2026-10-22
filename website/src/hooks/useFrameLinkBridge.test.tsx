import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useRef } from 'react'
import { OPEN_MESSAGE_TYPE } from './useFrameOpenLink'
import {
  checkEntry, FOCUS_ENTRY_CHECK_MS, focusEntry, frameInputs, hostInput, INPUT_MESSAGE_TYPE, tiedToFrame, useFrameLinkBridge, webUrlOf,
} from './useFrameLinkBridge'
import { FrameLinkChip } from '../components/FrameLinkChip'
import { LocalOverlayRoot } from '../components/overlayRoot'

const URL_A = 'https://github.com/example-org/repo/pull/7'

/** A host with one frame. The frame's window is a unique object: the hook compares identity only. */
function Host({ frame, open, gesture, tied }: {
  frame: HTMLIFrameElement
  open: (url: string) => void
  gesture?: (frame: HTMLIFrameElement) => boolean
  tied?: (frame: HTMLIFrameElement) => boolean
}) {
  const ref = useRef<HTMLIFrameElement | null>(frame)
  return <>{useFrameLinkBridge([ref], open, gesture ?? (() => true), tied ?? (() => true))}</>
}

function fakeFrame(): HTMLIFrameElement {
  return { contentWindow: {} as Window, blur: vi.fn() } as unknown as HTMLIFrameElement
}

/** A real, focusable iframe in its own holder, with a unique window. */
function realFrame(): { holder: HTMLDivElement; frame: HTMLIFrameElement; win: Window } {
  const holder = document.createElement('div')
  holder.setAttribute('data-test-holder', '')
  const frame = document.createElement('iframe')
  frame.tabIndex = 0
  holder.appendChild(frame)
  document.body.appendChild(holder)
  const win = {} as Window
  Object.defineProperty(frame, 'contentWindow', { configurable: true, value: win })
  return { holder, frame, win }
}

/** Focus moving from the dashboard into *frame*: the dashboard's window blurs. */
function enter(frame: HTMLIFrameElement) {
  frame.focus()
  window.dispatchEvent(new FocusEvent('blur'))
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  act(() => { window.dispatchEvent(event) })
}

const focused = (f: HTMLIFrameElement) => document.activeElement === f

beforeEach(() => {
  // No dashboard input in the last activation lifespan unless a test says so.
  hostInput.lastAt = -Infinity
  frameInputs.clear()
  focusEntry.frame = null
  focusEntry.pointer = null
  // Stops a watch an earlier test left running, so each test starts its own.
  checkEntry()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.querySelectorAll('[data-test-holder]').forEach((n) => n.remove())
})

describe('useFrameLinkBridge', () => {
  it('opens on a click tied to its frame, with no dialog', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(URL_A)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('opens nothing, and offers nothing, without a click in that frame', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} gesture={() => false} tied={() => false} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('opens once per click: focus moves to the frame holder, so a page posting again opens nothing', () => {
    const { holder, frame, win } = realFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} gesture={focused} />)
    frame.focus()
    expect(document.activeElement).toBe(frame)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    post({ type: OPEN_MESSAGE_TYPE, url: 'https://example.test/other' }, win)
    expect(open).toHaveBeenCalledTimes(1)
    // A keyboard user keeps their place: focus is on the frame's holder, one
    // Tab from the page, not dropped on the document body.
    expect(document.activeElement).toBe(holder)
    expect(holder.getAttribute('tabindex')).toBe('-1')
  })

  it('ignores a message from a window that is not its frame', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, {} as Window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, null)
    expect(open).not.toHaveBeenCalled()
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,<p>x</p>',
    'blob:https://example.test/0b1c',
    'file:///etc/hosts',
    'mailto:someone@example.test',
    'vscode://file/x',
    '/relative/path',
    'https://user:pass@example.test/',
    'https://github.com@evil.example/',
  ])('refuses %s', (url) => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
  })

  it('opens the parsed URL, so what the hover title shows is what opens', () => {
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'HTTPS://Example.TEST/a b' })).toBe('https://example.test/a%20b')
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'http://example.test' })).toBe('http://example.test/')
    expect(webUrlOf({ type: 'kirocrew-dashboard:act', url: URL_A })).toBeNull()
    const long = `https://example.test/${'a'.repeat(4096)}`
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: long })).toBe(long)
  })

  it('opens through window.open with noopener and noreferrer by default', () => {
    const frame = fakeFrame()
    const spy = vi.spyOn(window, 'open').mockImplementation(() => null)
    function DefaultHost() {
      const ref = useRef<HTMLIFrameElement | null>(frame)
      useFrameLinkBridge([ref], undefined, () => true, () => true)
      return null
    }
    render(<DefaultHost />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(spy).toHaveBeenCalledWith(URL_A, '_blank', 'noopener,noreferrer')
  })

  it('records the dashboard\'s own clicks and keys', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1234)
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    expect(hostInput.lastAt).toBe(1234)
    now.mockReturnValue(5678)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(hostInput.lastAt).toBe(5678)
  })

  it('records where focus came in from the dashboard, and the frame under the pointer', () => {
    const a = realFrame().frame
    const b = realFrame().frame
    enter(a)
    expect(focusEntry.frame).toBe(a)
    window.dispatchEvent(new FocusEvent('focus'))
    expect(focusEntry.frame).toBeNull()
    b.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    expect(focusEntry.pointer).toBe(b)
    document.body.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    expect(focusEntry.pointer).toBeNull()
  })
})

describe('tying the click to the asking frame', () => {
  /** The real tie and gesture test: the frame must hold focus. */
  function Real({ frame, open }: { frame: HTMLIFrameElement; open: (url: string) => void }) {
    const ref = useRef<HTMLIFrameElement | null>(frame)
    return <>{useFrameLinkBridge([ref], open, focused)}</>
  }

  const chip = () => screen.queryByRole('button', { name: /github\.com/ })

  it('opens at once on a click inside the frame', () => {
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(frame)
    expect(tiedToFrame(frame)).toBe(true)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).toHaveBeenCalledWith(URL_A)
    expect(chip()).toBeNull()
  })

  it('shows the button instead of opening when the click was in another frame', () => {
    const other = realFrame().frame
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    // Focus came into the other frame from the dashboard, then moved to this one
    // from that frame, which fires nothing in the dashboard.
    enter(other)
    frame.focus()
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).not.toHaveBeenCalled()
    const button = chip()
    expect(button).not.toBeNull()
    expect(button!.textContent).toBe('Open github.com')
    expect(button!.getAttribute('title')).toBe(URL_A)
    // It takes focus, so the frame cannot ask again on the same click.
    expect(document.activeElement).toBe(button)
  })

  it('opens when the person clicks the button, and the button goes', () => {
    const other = realFrame().frame
    const { holder, frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(other)
    frame.focus()
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    fireEvent.click(chip()!)
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(URL_A)
    expect(chip()).toBeNull()
    expect(document.activeElement).toBe(holder)
  })

  it.each([
    ['a click or key in the dashboard is still live', () => { hostInput.lastAt = performance.now() }],
    ['a sibling frame reported a click', () => { post({ type: INPUT_MESSAGE_TYPE }, {} as Window) }],
    ['the pointer is over another frame', (other: HTMLIFrameElement) => {
      other.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    }],
  ])('shows the button when %s', (_label, arrange) => {
    const other = realFrame().frame
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(frame)
    arrange(other)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).not.toHaveBeenCalled()
    expect(chip()).not.toBeNull()
  })

  it('does not count the frame\'s own reported input against it', () => {
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(frame)
    post({ type: INPUT_MESSAGE_TYPE }, win)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('forgets where focus came in once focus is seen in another frame, even if the frame takes it back', () => {
    // Focus came into this frame from the dashboard, then went to another frame (which
    // fires nothing in the dashboard), then this frame focused itself again.
    const other = realFrame().frame
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(frame)
    other.focus()
    // The dashboard hears something while focus is elsewhere: here a pointer move.
    other.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    expect(focusEntry.frame).toBeNull()
    frame.focus()
    frame.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).not.toHaveBeenCalled()
    expect(chip()).not.toBeNull()
  })

  it('forgets where focus came in once the pointer reaches another frame, where it may click', () => {
    const other = realFrame().frame
    const { frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    enter(frame)
    // Focus never visibly leaves: a page that takes it straight back looks like this.
    other.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    expect(focusEntry.frame).toBeNull()
    frame.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
    expect(open).not.toHaveBeenCalled()
    expect(chip()).not.toBeNull()
  })

  it('also notices focus leaving the frame on its own timer', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
    const other = realFrame().frame
    const { frame } = realFrame()
    enter(frame)
    other.focus()
    expect(focusEntry.frame).toBe(frame)
    act(() => { vi.advanceTimersByTime(FOCUS_ENTRY_CHECK_MS) })
    expect(focusEntry.frame).toBeNull()
  })

  it('takes the button away when its Crew is hidden behind a remote one', () => {
    const { frame } = realFrame()
    const onDismiss = vi.fn()
    const chipFor = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}>
        <FrameLinkChip url={URL_A} frame={frame} onOpen={vi.fn()} onDismiss={onDismiss} />
      </LocalOverlayRoot>
    )
    const { rerender } = render(chipFor(false))
    expect(onDismiss).not.toHaveBeenCalled()
    rerender(chipFor(true))
    expect(onDismiss).toHaveBeenCalled()
  })

  it('keeps the button however long it waits, and takes it away on a click elsewhere or Escape', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const other = realFrame().frame
    const { holder, frame, win } = realFrame()
    const open = vi.fn()
    render(<Real frame={frame} open={open} />)
    const ask = () => {
      enter(other)
      frame.focus()
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, win)
      expect(chip()).not.toBeNull()
    }
    ask()
    // No timer: a slow reader still finds it a minute later.
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(chip()).not.toBeNull()
    act(() => { document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true })) })
    expect(chip()).toBeNull()
    ask()
    act(() => { document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true })) })
    expect(chip()).toBeNull()
    ask()
    act(() => { fireEvent.keyDown(chip()!, { key: 'Escape' }) })
    expect(chip()).toBeNull()
    expect(document.activeElement).toBe(holder)
    expect(open).not.toHaveBeenCalled()
  })
})
