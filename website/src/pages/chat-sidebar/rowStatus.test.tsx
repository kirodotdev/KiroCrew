/**
 * The row-status store: a chat switch repaints only the rows whose answer flips.
 */
import { describe, it, expect } from 'vitest'
import { memo } from 'react'
import { act, render, renderHook } from '@testing-library/react'
import { RowStatusContext, createRowStatusStore, rowStatusOf, useActiveRowAmong, useRowStatus, useStableKeySet, type RowStatus } from './rowStatus'
import type { Slot } from './types'

const status = (over: Partial<RowStatus> = {}): RowStatus => ({
  activeSlot: null, crewWindow: null, crewParent: null, splitMode: false, openFailed: false, unread: new Set(), ...over,
})
const slots: Slot[] = Array.from({ length: 6 }, (_, i) => ({ key: `s${i}` } as Slot))

describe('row status', () => {
  it('re-renders only the rows whose own answer changed', () => {
    const store = createRowStatusStore(status({ activeSlot: 's1', unread: new Set(['s3']) }))
    const renders: Record<string, number> = {}
    const Row = memo(function Row({ slot }: { slot: Slot }) {
      renders[slot.key] = (renders[slot.key] ?? 0) + 1
      const { isActive, isUnread } = useRowStatus(slot)
      return <div data-testid={slot.key} data-active={isActive} data-unread={isUnread} />
    })
    const { getByTestId } = render(
      <RowStatusContext.Provider value={store}>{slots.map(s => <Row key={s.key} slot={s} />)}</RowStatusContext.Provider>,
    )
    expect(getByTestId('s1').dataset.active).toBe('true')
    expect(getByTestId('s3').dataset.unread).toBe('true')
    for (const k of Object.keys(renders)) renders[k] = 0

    // Open s3: s1 loses the highlight, s3 gains it and is read.
    act(() => { store.set(status({ activeSlot: 's3' })); store.publish() })
    expect(getByTestId('s1').dataset.active).toBe('false')
    expect(getByTestId('s3').dataset.active).toBe('true')
    expect(getByTestId('s3').dataset.unread).toBe('false')
    expect(renders).toEqual({ s0: 0, s1: 1, s2: 0, s3: 1, s4: 0, s5: 0 })
  })

  it('withholds the open-row reclick in Split View and after a failed open', () => {
    const slot = slots[2]
    expect(rowStatusOf(status({ activeSlot: 's2' }), slot).reclickPanel).toBe(true)
    expect(rowStatusOf(status({ activeSlot: 's2', splitMode: true }), slot).reclickPanel).toBe(false)
    expect(rowStatusOf(status({ activeSlot: 's2', openFailed: true }), slot).reclickPanel).toBe(false)
  })

  it('keeps the raw active test when a crew window covers the pane', () => {
    const slot = slots[2]
    const covered = rowStatusOf(status({ activeSlot: 's2', crewWindow: { instanceId: 'i', key: 'x' } }), slot)
    expect(covered.isActive).toBe(false)
    expect(covered.isSidebarActiveSlot).toBe(true)
  })

  it('hides a divider next to the open row from either side', () => {
    const [above, open, below] = slots
    const st = status({ activeSlot: open.key })
    expect(rowStatusOf(st, above, open).nextIsActive).toBe(true)
    expect(rowStatusOf(st, open, below).isActive).toBe(true)
    expect(rowStatusOf(st, below, slots[3]).nextIsActive).toBe(false)
    expect(rowStatusOf(st, below).nextIsActive).toBe(false)
  })

  it('keeps the unread set while its members hold', () => {
    const { result, rerender } = renderHook(({ keys }) => useStableKeySet(keys), { initialProps: { keys: ['a', 'b'] } })
    const first = result.current
    rerender({ keys: ['b', 'a'] })
    expect(result.current).toBe(first)
    rerender({ keys: ['a'] })
    expect([...result.current]).toEqual(['a'])
    expect(result.current).not.toBe(first)
  })

  it('reads the open row only among the rows it is given', () => {
    const store = createRowStatusStore(status({ activeSlot: 's0' }))
    const among = [slots[2], slots[3]]
    let renders = 0
    const { result } = renderHook(() => { renders++; return useActiveRowAmong(among) }, {
      wrapper: ({ children }) => <RowStatusContext.Provider value={store}>{children}</RowStatusContext.Provider>,
    })
    expect(result.current).toBeNull()
    renders = 0
    // A switch between two rows outside the set changes nothing here.
    act(() => { store.set(status({ activeSlot: 's1' })); store.publish() })
    expect(renders).toBe(0)
    act(() => { store.set(status({ activeSlot: 's3' })); store.publish() })
    expect(result.current).toBe(slots[3])
  })
})
