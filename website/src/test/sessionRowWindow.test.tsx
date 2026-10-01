/**
 * Row windowing (pages/chat/sessionRowWindow): a row far from its lane's
 * viewport renders a stub of the same height that keeps the row's DOM identity,
 * and mounts the real row when the lane's IntersectionObserver brings it near.
 *
 * jsdom has no IntersectionObserver, so each case installs a controllable one:
 * `fire(el, near, height)` delivers an entry for one observed element, which is
 * exactly the signal the production observer sends.
 */
import React, { useEffect, useState } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  SessionRowWindowContext, WindowedSessionRow, useSessionRowWindowRoot,
  SESSION_ROW_STUB_DEFAULT_PX, SESSION_ROW_WINDOW_MARGIN_PX, SESSION_ROW_INITIAL_MOUNT_BUDGET, _resetSessionRowWindowHeights,
} from '../pages/chat/sessionRowWindow'

type Cb = (entries: Array<Partial<IntersectionObserverEntry>>) => void
let observers: Array<{ cb: Cb; els: Set<Element>; opts?: IntersectionObserverInit }> = []

class MockIO {
  private rec: { cb: Cb; els: Set<Element>; opts?: IntersectionObserverInit }
  constructor(cb: Cb, opts?: IntersectionObserverInit) {
    this.rec = { cb, els: new Set(), opts }
    observers.push(this.rec)
  }
  observe(el: Element) { this.rec.els.add(el) }
  unobserve(el: Element) { this.rec.els.delete(el) }
  disconnect() { this.rec.els.clear() }
  takeRecords() { return [] }
}

function fire(el: Element, near: boolean, height = 0) {
  const o = observers.find(r => r.els.has(el))
  if (!o) throw new Error('element is not observed')
  act(() => o.cb([{ target: el, isIntersecting: near, boundingClientRect: { height } as DOMRectReadOnly }]))
}

const slotEl = (id: string) => document.querySelector(`[data-session-row="${id}"]`)!.closest('[data-session-window]')!

/** Stands in for SessionRow: counts mounts so a test can tell a remount from a
 *  re-render, and carries the real row's focus target. */
let mounts = 0
let rowClick = vi.fn()
let rowAuxClick = vi.fn()
let rowContextMenu = vi.fn()
let rowKeyDown = vi.fn()
function FakeRow({ id }: { id: string }) {
  useEffect(() => { mounts++ }, [])
  return (
    <button
      type="button"
      data-session-row={id}
      data-testid={`real-${id}`}
      onClick={rowClick}
      onAuxClick={rowAuxClick}
      onContextMenu={rowContextMenu}
      onKeyDown={rowKeyDown}
    >
      real {id}
    </button>
  )
}

/** A lane root with an initial-mount budget of `initial` rows (0 = every row
 *  starts as a stub), wrapping `ids` as windowed rows. */
function Lane({ ids, initial = 0, keep = '', attach = true, testId = 'lane' }: { ids: string[]; initial?: number; keep?: string; attach?: boolean; testId?: string }) {
  const { rowWindow, setRoot } = useSessionRowWindowRoot(initial)
  return (
    <SessionRowWindowContext.Provider value={rowWindow}>
      <div ref={attach ? setRoot : undefined} data-testid={testId}>
        {ids.map(id => (
          <WindowedSessionRow key={id} rowId={id} slotKey={`slot-${id}`} navScope="list" holdContainer="tree:root" title={`Title ${id}`}
            keepMounted={id === keep}>
            <FakeRow id={id} />
          </WindowedSessionRow>
        ))}
      </div>
    </SessionRowWindowContext.Provider>
  )
}

describe('WindowedSessionRow', () => {
  beforeEach(() => {
    observers = []
    mounts = 0
    rowClick = vi.fn()
    rowAuxClick = vi.fn()
    rowContextMenu = vi.fn()
    rowKeyDown = vi.fn()
    _resetSessionRowWindowHeights()
    vi.stubGlobal('IntersectionObserver', MockIO)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('renders the row unchanged, with no wrapper, where IntersectionObserver is missing', () => {
    vi.unstubAllGlobals()
    vi.stubGlobal('IntersectionObserver', undefined)
    render(<Lane ids={['a', 'b']} />)
    expect(screen.getByTestId('real-a').parentElement).toBe(screen.getByTestId('lane'))
    expect(document.querySelector('[data-session-window]')).toBeNull()
  })

  it('roots one observer on the lane with the window margin', () => {
    render(<Lane ids={['a', 'b', 'c']} />)
    expect(observers).toHaveLength(1)
    expect(observers[0].opts?.root).toBe(screen.getByTestId('lane'))
    expect(observers[0].opts?.rootMargin).toBe(`${SESSION_ROW_WINDOW_MARGIN_PX}px 0px`)
    expect(observers[0].els.size).toBe(3)
  })

  it('stubs a far row with the real row\'s DOM identity, then mounts it when it comes near', () => {
    render(<Lane ids={['a', 'b']} initial={1} />)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(screen.queryByTestId('real-b')).toBeNull()
    const stub = document.querySelector<HTMLElement>('[data-session-row="b"]')!
    expect(stub.dataset.sessionScope).toBe('list')
    expect(stub.dataset.sessionContainer).toBe('tree:root')
    // The real row's outer box carries data-slot-key; so does the stub's.
    expect(document.querySelector('[data-slot-key="slot-b"] [data-session-row="b"]')).toBe(stub)
    expect(stub.textContent).toBe('\u00A0Title b')
    expect(stub.firstElementChild).toHaveAttribute('aria-hidden', 'true')
    // Accessible name: the stub is a tab stop, so it must read as the session.
    expect(stub.getAttribute('title')).toBe('Title b')
    expect(stub.style.height).toBe(`${SESSION_ROW_STUB_DEFAULT_PX}px`)
    // DOM order is intact: row walks (keyboard nav, digit shortcuts) see both.
    expect(Array.from(document.querySelectorAll('[data-session-row]')).map(e => e.getAttribute('data-session-row'))).toEqual(['a', 'b'])

    fire(slotEl('b'), true)
    expect(screen.getByTestId('real-b')).toBeTruthy()
  })

  it('stubs a row that leaves the window at the height it last measured', () => {
    render(<Lane ids={['a']} initial={1} />)
    fire(slotEl('a'), false, 91)
    expect(screen.queryByTestId('real-a')).toBeNull()
    expect(document.querySelector<HTMLElement>('[data-session-row="a"]')!.style.height).toBe('91px')
  })

  it('ignores a zero-height "not intersecting" entry: a collapsed folder\'s rows keep their state and last real height', () => {
    // FolderBody keeps a collapsed folder's rows mounted but un-rendered
    // (content-visibility: hidden), so the observer reports every one of them
    // not intersecting at height 0, wherever the folder sits. That is neither a
    // size nor a position: a live row stays live. A zero-height NEAR entry
    // still mounts (the repo's test IO stub fires exactly that), since a spare
    // mount is the safe side to err on.
    render(<Lane ids={['a', 'b']} initial={1} />)
    fire(slotEl('a'), true, 48)
    fire(slotEl('a'), false, 0)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    fire(slotEl('b'), true, 0)
    expect(screen.getByTestId('real-b')).toBeTruthy()
    // Once laid out again the row is measured for real and the window applies.
    fire(slotEl('a'), false, 55)
    expect(screen.queryByTestId('real-a')).toBeNull()
    expect(document.querySelector<HTMLElement>('[data-session-row="a"]')!.style.height).toBe('55px')
  })

  it('mounts the first rows of each window root, not of the lane as a whole', () => {
    // Board columns are separate roots; each must show real rows at first
    // paint, not just the column that happened to render first.
    render(
      <>
        <Lane ids={['a', 'b', 'c']} initial={2} testId="lane-1" />
        <Lane ids={['d', 'e', 'f']} initial={2} testId="lane-2" />
      </>,
    )
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(screen.getByTestId('real-b')).toBeTruthy()
    expect(screen.queryByTestId('real-c')).toBeNull()
    expect(screen.getByTestId('real-d')).toBeTruthy()
    expect(screen.getByTestId('real-e')).toBeTruthy()
    expect(screen.queryByTestId('real-f')).toBeNull()
  })

  it('mounts SESSION_ROW_INITIAL_MOUNT_BUDGET rows by default', () => {
    const ids = Array.from({ length: SESSION_ROW_INITIAL_MOUNT_BUDGET + 3 }, (_, i) => `r${i}`)
    function DefaultLane() {
      const { rowWindow, setRoot } = useSessionRowWindowRoot()
      return (
        <SessionRowWindowContext.Provider value={rowWindow}>
          <div ref={setRoot}>
            {ids.map(id => (
              <WindowedSessionRow key={id} rowId={id} slotKey={id} navScope="list" holdContainer="c" title={id} keepMounted={false}><FakeRow id={id} /></WindowedSessionRow>
            ))}
          </div>
        </SessionRowWindowContext.Provider>
      )
    }
    render(<DefaultLane />)
    expect(mounts).toBe(SESSION_ROW_INITIAL_MOUNT_BUDGET)
    expect(screen.getByTestId(`real-r${SESSION_ROW_INITIAL_MOUNT_BUDGET - 1}`)).toBeTruthy()
    expect(screen.queryByTestId(`real-r${SESSION_ROW_INITIAL_MOUNT_BUDGET}`)).toBeNull()
  })

  it('spends one budget position per row under StrictMode\'s double-invoked initializers', () => {
    render(<React.StrictMode><Lane ids={['a', 'b', 'c']} initial={2} /></React.StrictMode>)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(screen.getByTestId('real-b')).toBeTruthy()
    expect(screen.queryByTestId('real-c')).toBeNull()
  })

  it('hands each render pass of the root a fresh budget', () => {
    // Mounted rows keep their state across a re-render (no initializer re-runs);
    // a row added in a later pass counts from zero for that pass, so it starts
    // live and the observer's immediate first delivery settles it. Over-mounting
    // a few rows is the cheap side to err on.
    const { rerender } = render(<Lane ids={['a', 'b']} initial={2} />)
    rerender(<Lane ids={['a', 'b', 'c']} initial={2} />)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(screen.getByTestId('real-b')).toBeTruthy()
    expect(screen.getByTestId('real-c')).toBeTruthy()
    expect(mounts).toBe(3)
  })

  it('gives the stub the real row\'s tab stop, so Tab still walks every session', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(stub.getAttribute('role')).toBe('button')
    expect(stub.tabIndex).toBe(0)
  })

  it('mounts a clicked stub and forwards the click modifiers to the real row once', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    fireEvent.click(stub, { metaKey: true, ctrlKey: true, clientX: 17, clientY: 23 })

    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(rowClick).toHaveBeenCalledTimes(1)
    expect(rowClick.mock.calls[0][0].metaKey).toBe(true)
    expect(rowClick.mock.calls[0][0].ctrlKey).toBe(true)
  })

  it('mounts a context-clicked stub and forwards its coordinates to the real row once', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    const event = new MouseEvent('contextmenu', {
      button: 2,
      buttons: 2,
      clientX: 17,
      clientY: 23,
      bubbles: true,
      cancelable: true,
    })
    act(() => { stub.dispatchEvent(event) })

    expect(event.defaultPrevented).toBe(true)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(rowContextMenu).toHaveBeenCalledTimes(1)
    expect(rowContextMenu.mock.calls[0][0].clientX).toBe(17)
    expect(rowContextMenu.mock.calls[0][0].clientY).toBe(23)
  })

  it('keeps the stub through a real press so the following click reaches the real row', () => {
    // A browser focuses a tabIndex=0 element on mousedown unless the press is
    // default-prevented; focusing the stub would mount the row before mouseup
    // and send `click` to the wrapper instead. Model the real sequence.
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    const down = new MouseEvent('mousedown', { button: 0, bubbles: true, cancelable: true })
    act(() => { stub.dispatchEvent(down) })
    expect(down.defaultPrevented).toBe(true)
    // Prevented: the browser would not focus, so the stub is still there.
    expect(stub.isConnected).toBe(true)
    expect(screen.queryByTestId('real-a')).toBeNull()
    fireEvent.mouseUp(stub, { button: 0 })
    fireEvent.click(stub, { button: 0 })

    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(rowClick).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(screen.getByTestId('real-a'))
  })

  it('mounts a middle-clicked stub and forwards button 1 to the real row once', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(fireEvent.mouseDown(stub, { button: 1 })).toBe(false)
    fireEvent(stub, new MouseEvent('auxclick', { button: 1, bubbles: true, cancelable: true }))

    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(rowAuxClick).toHaveBeenCalledTimes(1)
    expect(rowAuxClick.mock.calls[0][0].button).toBe(1)
    expect(rowClick).not.toHaveBeenCalled()
  })

  it('forwards Enter when focus and keydown reach the stub before its mount commits', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    act(() => {
      stub.focus()
      stub.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }))
    })

    expect(document.activeElement).toBe(screen.getByTestId('real-a'))
    expect(rowKeyDown).toHaveBeenCalledTimes(1)
    expect(rowKeyDown.mock.calls[0][0].key).toBe('Enter')
  })

  it('keeps a keepMounted row live wherever it is, and stubs it once the hold drops', () => {
    const { rerender } = render(<Lane ids={['a']} keep="a" />)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    fire(slotEl('a'), false, 64)
    expect(screen.getByTestId('real-a')).toBeTruthy()
    rerender(<Lane ids={['a']} />)
    expect(screen.queryByTestId('real-a')).toBeNull()
  })

  it('stubs an off-window row after its focus hold is released', async () => {
    render(<><Lane ids={['a']} initial={1} /><button data-testid="outside">outside</button></>)
    act(() => screen.getByTestId('real-a').focus())
    fire(slotEl('a'), false, 64)
    expect(screen.getByTestId('real-a')).toBeTruthy()

    act(() => screen.getByTestId('outside').focus())
    await waitFor(() => expect(screen.queryByTestId('real-a')).toBeNull())
    expect(slotEl('a')).toHaveAttribute('data-session-window', 'stub')
  })

  it('stubs an off-window row after its open menu closes', async () => {
    function RowWithMenu({ open }: { open: boolean }) {
      return <div data-session-row="a" data-testid="real-a"><button data-state={open ? 'open' : 'closed'}>menu</button></div>
    }
    function MenuLane({ open }: { open: boolean }) {
      const { rowWindow, setRoot } = useSessionRowWindowRoot()
      return (
        <SessionRowWindowContext.Provider value={rowWindow}>
          <div ref={setRoot}>
            <WindowedSessionRow rowId="a" slotKey="slot-a" navScope="list" holdContainer="c" title="A" keepMounted={false}><RowWithMenu open={open} /></WindowedSessionRow>
          </div>
        </SessionRowWindowContext.Provider>
      )
    }
    const { rerender } = render(<MenuLane open />)
    fire(slotEl('a'), false, 64)
    expect(screen.getByTestId('real-a')).toBeTruthy()

    rerender(<MenuLane open={false} />)
    await waitFor(() => expect(screen.queryByTestId('real-a')).toBeNull())
    expect(slotEl('a')).toHaveAttribute('data-session-window', 'stub')
  })

  it('keeps a held row mounted on release after it comes near again', async () => {
    render(<><Lane ids={['a']} initial={1} /><button data-testid="outside">outside</button></>)
    act(() => screen.getByTestId('real-a').focus())
    fire(slotEl('a'), false, 64)
    fire(slotEl('a'), true, 64)

    act(() => screen.getByTestId('outside').focus())
    await act(async () => { await Promise.resolve() })
    expect(screen.getByTestId('real-a')).toBeTruthy()
    expect(slotEl('a')).toHaveAttribute('data-session-window', 'live')
  })

  it('moves focus from a stub onto the real row once it mounts', () => {
    render(<Lane ids={['a']} />)
    const stub = document.querySelector<HTMLElement>('[data-session-row="a"]')!
    act(() => stub.focus())
    expect(document.activeElement).toBe(screen.getByTestId('real-a'))
  })

  it('does not remount rows when the observer root attaches after them', () => {
    // The lane root is set by a ref callback in the same commit as the rows, but
    // a root that arrives LATER (a lane switch, a remounted scroller) must not
    // change the context value and remount every row below it.
    function LateRoot() {
      const { rowWindow, setRoot } = useSessionRowWindowRoot()
      const [attached, setAttached] = useState(false)
      useEffect(() => { setAttached(true) }, [])
      return (
        <SessionRowWindowContext.Provider value={rowWindow}>
          <div ref={attached ? setRoot : undefined}>
            <WindowedSessionRow rowId="a" slotKey="slot-a" navScope="list" holdContainer="c" title="A" keepMounted={false}><FakeRow id="a" /></WindowedSessionRow>
          </div>
        </SessionRowWindowContext.Provider>
      )
    }
    render(<LateRoot />)
    expect(mounts).toBe(1)
    expect(observers).toHaveLength(1)
    expect(observers[0].els.size).toBe(1)
  })
})
