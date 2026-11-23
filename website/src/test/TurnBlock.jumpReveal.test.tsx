/**
 * A jump's target inside a step fold opens the fold, as an in-chat search match
 * does, so the reader is not scrolled to a highlight drawn at zero height.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import JumpRevealContext from '../hooks/JumpRevealContext'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const renderItem = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)

/** An interim fan-out region: both rows fold, so one toggle is rendered. */
const foldedTurn = (): Extract<DisplayItem, { kind: 'turn' }> => ({
  kind: 'turn',
  complete: true,
  interim: true,
  items: [
    { kind: 'single', msg: { role: 'assistant', content: 'Two of three agents are in…', ts: '1' }, idx: 0 },
    { kind: 'single', msg: { role: 'assistant', content: 'and the third just landed', ts: '2' }, idx: 1 },
  ],
})

const toggle = (c: HTMLElement) => c.querySelector('button[aria-expanded]')!

describe('TurnBlock — a jump into the fold', () => {
  it('stays folded with no jump target', () => {
    const { container } = render(<TurnBlock turn={foldedTurn()} renderItem={renderItem} />)
    expect(toggle(container).getAttribute('aria-expanded')).toBe('false')
  })

  it('opens when the jump target is a folded row', () => {
    const { container } = render(
      <JumpRevealContext.Provider value={{ ts: "2", seq: 0, claimScroll: () => {}, scrollerEl: () => null }}>
        <TurnBlock turn={foldedTurn()} renderItem={renderItem} />
      </JumpRevealContext.Provider>,
    )
    expect(toggle(container).getAttribute('aria-expanded')).toBe('true')
  })

  it('stays folded when the jump target is in another turn', () => {
    const { container } = render(
      <JumpRevealContext.Provider value={{ ts: "99", seq: 0, claimScroll: () => {}, scrollerEl: () => null }}>
        <TurnBlock turn={foldedTurn()} renderItem={renderItem} />
      </JumpRevealContext.Provider>,
    )
    expect(toggle(container).getAttribute('aria-expanded')).toBe('false')
  })
})

describe('TurnBlock — scrolling to the revealed row', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('ends the jump scroll, then scrolls the revealed row itself into view', () => {
    vi.useFakeTimers()
    const scrolled: string[] = []
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
      scrolled.push((this as HTMLElement).dataset.jumpTarget ?? '')
    })
    const claimScroll = vi.fn()
    const markedItem = (it: TurnItem) => (
      <div data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    )
    render(
      <JumpRevealContext.Provider value={{ ts: '2', seq: 0, claimScroll, scrollerEl: () => null }}>
        <TurnBlock turn={foldedTurn()} renderItem={markedItem} />
      </JumpRevealContext.Provider>,
    )
    expect(claimScroll).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(250) })
    expect(claimScroll).toHaveBeenCalledTimes(1)
    expect(scrolled).toEqual(['2'])
  })

  it('scrolls the transcript row, not a copy of its marker authored in an earlier message', () => {
    vi.useFakeTimers()
    const scrolled: Element[] = []
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
      scrolled.push(this)
    })
    // The earlier reply's content mints the quoted row's marker ahead of it in document order.
    const forgingItem = (it: TurnItem) => it.kind === 'single' && it.msg.ts === '1'
      ? <div data-message-body=""><span data-jump-target="2">{it.msg.content}</span></div>
      : <div data-testid="real-row" data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    const { getByTestId } = render(
      <JumpRevealContext.Provider value={{ ts: '2', mid: null, seq: 0, claimScroll: () => {}, scrollerEl: () => null }}>
        <TurnBlock turn={foldedTurn()} renderItem={forgingItem} />
      </JumpRevealContext.Provider>,
    )
    act(() => { vi.advanceTimersByTime(250) })
    expect(scrolled).toHaveLength(1)
    expect(scrolled[0]).toBe(getByTestId('real-row'))
  })
})

describe('TurnBlock — a jump target the fold does not hide', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('still scrolls the row itself into view, without a fold to open', () => {
    vi.useFakeTimers()
    const scrolled: string[] = []
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
      scrolled.push((this as HTMLElement).dataset.jumpTarget ?? '')
    })
    const claimScroll = vi.fn()
    const markedItem = (it: TurnItem) => (
      <div data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    )
    const flat: Extract<DisplayItem, { kind: 'turn' }> = {
      kind: 'turn',
      complete: true,
      items: [
        { kind: 'single', msg: { role: 'assistant', content: 'first reply', ts: 'x1' }, idx: 0 },
        { kind: 'single', msg: { role: 'assistant', content: 'second reply', ts: 'x2' }, idx: 1 },
      ],
    }
    const { container } = render(
      <JumpRevealContext.Provider value={{ ts: 'x1', seq: 0, claimScroll, scrollerEl: () => null }}>
        <TurnBlock turn={flat} renderItem={markedItem} />
      </JumpRevealContext.Provider>,
    )
    expect(container.querySelector('button[aria-expanded]')).toBeNull()
    act(() => { vi.advanceTimersByTime(250) })
    expect(claimScroll).toHaveBeenCalledTimes(1)
    expect(scrolled).toEqual(['x1'])
  })
})

describe('TurnBlock — a reader who scrolls first keeps their place', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('cancels the pending row scroll on a wheel over the transcript', () => {
    vi.useFakeTimers()
    const scrolled = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    const claimScroll = vi.fn()
    const scroller = document.createElement('div')
    document.body.appendChild(scroller)
    const markedItem = (it: TurnItem) => (
      <div data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    )
    try {
      render(
        <JumpRevealContext.Provider value={{ ts: '2', seq: 0, claimScroll, scrollerEl: () => scroller }}>
          <TurnBlock turn={foldedTurn()} renderItem={markedItem} />
        </JumpRevealContext.Provider>,
      )
      act(() => { scroller.dispatchEvent(new WheelEvent('wheel', { deltaY: 40, bubbles: true })) })
      act(() => { vi.advanceTimersByTime(250) })
      expect(claimScroll).not.toHaveBeenCalled()
      expect(scrolled).not.toHaveBeenCalled()
    } finally {
      scroller.remove()
    }
  })
})

describe('TurnBlock — a repeat jump to the same row', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('scrolls the row again when the jump count moves on the same target', () => {
    vi.useFakeTimers()
    const scrolled = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    const claimScroll = vi.fn()
    const markedItem = (it: TurnItem) => (
      <div data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    )
    // One scroller getter for both renders, as ChatPage's memo keeps it, so only
    // the count changes between the two jumps.
    const scrollerEl = () => null
    const tree = (seq: number) => (
      <JumpRevealContext.Provider value={{ ts: '2', seq, claimScroll, scrollerEl }}>
        <TurnBlock turn={foldedTurn()} renderItem={markedItem} />
      </JumpRevealContext.Provider>
    )
    const { rerender } = render(tree(1))
    act(() => { vi.advanceTimersByTime(250) })
    expect(scrolled).toHaveBeenCalledTimes(1)
    rerender(tree(2))
    act(() => { vi.advanceTimersByTime(250) })
    expect(scrolled).toHaveBeenCalledTimes(2)
    expect(claimScroll).toHaveBeenCalledTimes(2)
  })
})

describe('TurnBlock — two panes showing the same transcript', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('scrolls the row in its own pane, not the same row in another pane', () => {
    vi.useFakeTimers()
    const scrolled: Element[] = []
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
      scrolled.push(this)
    })
    // Another pane, earlier in the document, holds a row with the same marker.
    const other = document.createElement('div')
    other.innerHTML = '<div data-jump-target="2">other pane</div>'
    document.body.appendChild(other)
    const markedItem = (it: TurnItem) => (
      <div data-jump-target={it.kind === 'single' ? it.msg.ts : undefined}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
    )
    let scroller: HTMLElement | null = null
    try {
      render(
        <div ref={el => { scroller = el }}>
          <JumpRevealContext.Provider value={{ ts: '2', seq: 0, claimScroll: () => {}, scrollerEl: () => scroller }}>
            <TurnBlock turn={foldedTurn()} renderItem={markedItem} />
          </JumpRevealContext.Provider>
        </div>,
      )
      act(() => { vi.advanceTimersByTime(250) })
      expect(scrolled).toHaveLength(1)
      expect(scroller!.contains(scrolled[0])).toBe(true)
    } finally {
      other.remove()
    }
  })
})
