/**
 * The streaming-path state writers that run on every chunk or commit schedule
 * nothing when the value would not change. React cannot always drop such an
 * update before rendering: when the component still has work pending it runs
 * the whole body to find out, which is what made ChatPage's body run about
 * twice per streamed chunk.
 *
 * `react`'s useState is wrapped so the test can count NO-OP calls: a setter
 * called with the value it already holds, or an updater that returns it.
 */
import { act, render, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const noop = vi.hoisted(() => ({ count: 0 }))

vi.mock('react', async (importOriginal) => {
  const R = await importOriginal<typeof import('react')>()
  const useState = ((init: unknown) => {
    const [value, set] = R.useState(init)
    const latest = R.useRef(value)
    latest.current = value
    const wrapped = R.useCallback((action: unknown) => {
      const next = typeof action === 'function' ? (action as (p: unknown) => unknown)(latest.current) : action
      if (Object.is(next, latest.current)) noop.count++
      set(action as never)
    }, [])
    return [value, wrapped]
  }) as typeof R.useState
  return { ...R, default: { ...R, useState }, useState }
})

import { useStreamIdle } from '../pages/chat/ChatFooter'
import { useScrollerElement } from '../hooks/virtualizer/observers'
import { usePinnedPrompt } from '../pages/chat/usePinnedPrompt'
import AssistantMessage from '../pages/chat/AssistantMessage'

beforeEach(() => { noop.count = 0 })
afterEach(() => { vi.useRealTimers() })

describe('useStreamIdle', () => {
  it('turns idle once the tick holds still, clears on the next tick, and is never idle while inactive', () => {
    vi.useFakeTimers()
    const { result, rerender } = renderHook(({ tick, active }) => useStreamIdle(tick, active, 1000), { initialProps: { tick: 0, active: true } })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe(true)
    rerender({ tick: 1, active: true })
    expect(result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(999) })
    expect(result.current).toBe(false)
    rerender({ tick: 1, active: false })
    act(() => { vi.advanceTimersByTime(5000) })
    expect(result.current).toBe(false)
  })

  it('schedules no update per tick while text keeps flowing', () => {
    vi.useFakeTimers()
    const { rerender } = renderHook(({ tick }) => useStreamIdle(tick, true, 1000), { initialProps: { tick: 0 } })
    for (let tick = 1; tick <= 20; tick++) rerender({ tick })
    expect(noop.count).toBe(0)
  })
})

describe('useScrollerElement', () => {
  it('promotes the scroller node once per element, not once per commit', () => {
    const ref = { current: document.createElement('div') }
    const { result } = renderHook(() => useScrollerElement(ref))
    for (let i = 0; i < 10; i++) act(() => { result.current.syncScrollerEl() })
    expect(result.current.scrollerEl).toBe(ref.current)
    expect(noop.count).toBe(0)
    const next = document.createElement('div')
    ref.current = next
    act(() => { result.current.syncScrollerEl() })
    expect(result.current.scrollerEl).toBe(next)
  })
})

describe('usePinnedPrompt', () => {
  it('schedules nothing when a recompute lands on the state it already has', () => {
    const { result } = renderHook(() => usePinnedPrompt({ scrollerRef: { current: null }, requiresMountedHandoff: false }))
    for (let i = 0; i < 10; i++) act(() => { result.current.updatePinnedPrompt() })
    act(() => { result.current.setPinned(prev => prev) })
    act(() => { result.current.setPinned(null) })
    expect(result.current.pinned).toBeNull()
    expect(noop.count).toBe(0)
  })
})

describe('AssistantMessage', () => {
  it('schedules no reset when its content changes and nothing was held', () => {
    const { rerender } = render(<AssistantMessage content="one" isStreaming={false} slotRunning={false} />)
    noop.count = 0
    for (const content of ['one two', 'one two three', 'one two three four']) {
      rerender(<AssistantMessage content={content} isStreaming={false} slotRunning={false} />)
    }
    expect(noop.count).toBe(0)
  })
})
