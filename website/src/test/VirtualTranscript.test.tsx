/**
 * VirtualTranscript (chat-core P5-e): the reusable virtualized transcript.
 *
 * jsdom has no layout, so the real virtualizer mounts its initial window — the
 * tail (or head) of `overscan + 1` rows — and never expands it. That is the
 * property these tests lean on: a long list renders a bounded number of rows,
 * while a short one renders every row in order.
 */
import { render, screen, fireEvent, act } from '@testing-library/react'
import React, { createRef } from 'react'
import VirtualTranscript, {
  type VirtualTranscriptHandle,
} from '../chat-core/transcript/VirtualTranscript'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

function msg(i: number, role = 'assistant'): ChatMessage {
  return { role, content: `m${i}`, cls: '', ts: `2026-01-01T00:00:${String(i).padStart(2, '0')}Z` }
}

function singles(n: number): DisplayItem[] {
  return Array.from({ length: n }, (_, i) => ({ kind: 'single' as const, msg: msg(i), idx: i }))
}

const renderRow = (it: DisplayItem) => (
  <div data-testid="row">{it.kind === 'single' ? it.msg.content : it.kind}</div>
)

function mountedIndices(container: HTMLElement): number[] {
  return [...container.querySelectorAll('[data-display-index]')]
    .map((el) => Number(el.getAttribute('data-display-index')))
}

describe('VirtualTranscript', () => {
  it('mounts every row of a short transcript, indexed in display order', () => {
    const { container } = render(
      <VirtualTranscript items={singles(5)} renderRow={renderRow} sessionId="t:short" />,
    )
    expect(mountedIndices(container)).toEqual([0, 1, 2, 3, 4])
    expect(screen.getAllByTestId('row').map((el) => el.textContent)).toEqual(['m0', 'm1', 'm2', 'm3', 'm4'])
  })

  it('renders a bounded window of a long transcript, anchored at the bottom', () => {
    const { container } = render(
      <VirtualTranscript items={singles(40)} renderRow={renderRow} sessionId="t:long" />,
    )
    const idx = mountedIndices(container)
    // The DOM cost is the window, not the history. (Where the window sits is a
    // layout question jsdom cannot answer; the browser rig covers that.)
    expect(idx.length).toBeGreaterThan(0)
    expect(idx.length).toBeLessThan(20)
    // One contiguous run of rows, never a scattered subset.
    expect(idx).toEqual(Array.from({ length: idx.length }, (_, k) => idx[0] + k))
    // The unmounted rows are represented by the shell's spacers.
    expect(container.querySelectorAll('.vc-spacer-skeleton')).toHaveLength(2)
  })

  it('opens at the top for the list contract (top-anchored embed: no follow, top placement)', () => {
    const { container } = render(
      <VirtualTranscript
        items={singles(40)}
        renderRow={renderRow}
        sessionId="t:top"
        initialPlacement="top"
        followOutput={false}
      />,
    )
    expect(mountedIndices(container)[0]).toBe(0)
  })

  it('owns the scroller with the shared style contract and no page header band', () => {
    const { container } = render(
      <VirtualTranscript
        items={singles(2)}
        renderRow={renderRow}
        sessionId="t:shell"
        scrollerStyle={{ paddingTop: 12 }}
      />,
    )
    const scroller = container.querySelector<HTMLDivElement>('.chat-container')
    expect(scroller).not.toBeNull()
    expect(scroller!.style.overflowY).toBe('auto')
    expect(scroller!.style.overflowX).toBe('hidden')
    // Host padding travels through scrollerStyle...
    expect(scroller!.style.paddingTop).toBe('12px')
    // ...and the main page's overlay-header spacer is not reserved here.
    expect(container.querySelector('.h-16')).toBeNull()
  })

  it('renders the earlier-history bar only while more exists, and routes the press to the host', () => {
    const onLoad = vi.fn()
    const { rerender } = render(
      <VirtualTranscript
        items={singles(3)}
        renderRow={renderRow}
        sessionId="t:earlier"
        earlier={{ hasMore: true, loading: false, failed: false, onLoad }}
      />,
    )
    fireEvent.click(screen.getByTestId('load-earlier-messages'))
    expect(onLoad).toHaveBeenCalledTimes(1)

    rerender(
      <VirtualTranscript
        items={singles(3)}
        renderRow={renderRow}
        sessionId="t:earlier"
        earlier={{ hasMore: false, loading: false, failed: false, onLoad }}
      />,
    )
    expect(screen.queryByTestId('load-earlier-messages')).toBeNull()
  })

  it('places host content above the rows and below them', () => {
    const { container } = render(
      <VirtualTranscript
        items={singles(2)}
        renderRow={renderRow}
        sessionId="t:slots"
        aboveRows={<div data-testid="above" />}
        belowRows={<div data-testid="below" />}
      />,
    )
    const order = [...container.querySelectorAll('[data-testid="above"], [data-display-index], [data-testid="below"]')]
      .map((el) => el.getAttribute('data-testid') ?? `row${el.getAttribute('data-display-index')}`)
    expect(order).toEqual(['above', 'row0', 'row1', 'below'])
  })

  it('hides a row by visibility when the host asks, keeping its box in flow', () => {
    const { container } = render(
      <VirtualTranscript
        items={singles(3)}
        renderRow={renderRow}
        sessionId="t:hidden"
        isRowHidden={(_it, i) => i === 1}
      />,
    )
    const rows = container.querySelectorAll<HTMLDivElement>('[data-display-index]')
    expect(rows[1].style.visibility).toBe('hidden')
    expect(rows[0].style.visibility).toBe('')
    expect(rows).toHaveLength(3)
  })

  it('exposes scroll-to-bottom through its handle', () => {
    const ref = createRef<VirtualTranscriptHandle>()
    render(<VirtualTranscript ref={ref} items={singles(3)} renderRow={renderRow} sessionId="t:handle" />)
    expect(ref.current).not.toBeNull()
    act(() => { ref.current!.scrollToBottom('auto') })
  })

  it('shares an external scroller ref with the host and reports the at-bottom state', () => {
    const scrollerRef = createRef<HTMLDivElement | null>() as React.MutableRefObject<HTMLDivElement | null>
    const onAtBottomChange = vi.fn()
    render(
      <VirtualTranscript
        items={singles(2)}
        renderRow={renderRow}
        sessionId="t:ext"
        scrollerRef={scrollerRef}
        onAtBottomChange={onAtBottomChange}
      />,
    )
    expect(scrollerRef.current?.classList.contains('chat-container')).toBe(true)
    expect(onAtBottomChange).toHaveBeenCalled()
    expect(typeof onAtBottomChange.mock.calls[0][0]).toBe('boolean')
  })
})
