/**
 * The fork-point index (which children were cut at which parent row) and the
 * marker the parent's transcript draws under that row.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage, ChatSlot } from '../../types'
import ForkPointMarker from './ForkPointMarker'
import { EMPTY_FORK_POINTS, forkChildrenAt, forkPointsOf, forkPointsSignature, type ForkChild } from './forkPoints'

const slot = (over: Partial<ChatSlot>) => ({ messages: 0, running: false, ...over }) as ChatSlot
const row = (ts: string, mid?: string) =>
  ({ role: 'assistant', content: 'x', ts, cls: '', ...(mid ? { meta: { mid } } : {}) }) as unknown as ChatMessage

const SLOTS = [
  slot({ key: 'parent', title: 'Parent' }),
  slot({ key: 'a', title: 'Fork A', forked_from: 'dashboard:parent', fork_point_mid: 'm-2', fork_point_ts: 't2', fork_direction: 'head' }),
  slot({ key: 'b', title: 'Fork B', forked_from: 'dashboard:parent', fork_point_mid: 'm-2', fork_point_ts: 't2', fork_direction: 'tail' }),
  slot({ key: 'c', title: 'Old row', forked_from: 'dashboard:parent', fork_point_mid: null, fork_point_ts: 't1', fork_direction: 'head' }),
  // A fork made before the point was recorded: it names its parent and nothing more.
  slot({ key: 'legacy', title: 'Legacy fork', forked_from: 'dashboard:parent' }),
  slot({ key: 'other', title: 'Elsewhere', forked_from: 'dashboard:someone-else', fork_point_mid: 'm-2', fork_direction: 'head' }),
]

describe('forkPointsOf', () => {
  it('indexes the children of one parent by the row they were cut after', () => {
    const index = forkPointsOf(SLOTS, 'parent')
    expect(forkChildrenAt(index, row('t2', 'm-2')).map(f => [f.key, f.direction])).toEqual([['a', 'head'], ['b', 'tail']])
    expect(forkChildrenAt(index, row('t9', 'm-9'))).toEqual([])
  })

  it('finds a child that recorded no id by the row timestamp', () => {
    expect(forkChildrenAt(forkPointsOf(SLOTS, 'parent'), row('t1')).map(f => f.key)).toEqual(['c'])
  })

  it('draws nothing for a fork without a recorded point, as before', () => {
    const index = forkPointsOf([SLOTS[0], SLOTS[4]], 'parent')
    expect(index).toBe(EMPTY_FORK_POINTS)
    expect(forkPointsSignature([SLOTS[0], SLOTS[4]], 'parent')).toBe('')
  })

  it('ignores children of other sessions and a session with no key', () => {
    const index = forkPointsOf(SLOTS, 'parent')
    expect([...index.byMid.values()].flat().map(f => f.key)).not.toContain('other')
    expect(forkPointsOf(SLOTS, null)).toBe(EMPTY_FORK_POINTS)
  })

  it('keeps one signature across a slot-list frame that changes nothing a marker shows', () => {
    const before = forkPointsSignature(SLOTS, 'parent')
    const busy = SLOTS.map(s => ({ ...s, running: true, messages: 7 }))
    expect(forkPointsSignature(busy, 'parent')).toBe(before)
    const renamed = SLOTS.map(s => (s.key === 'a' ? { ...s, title: 'Renamed' } : s))
    expect(forkPointsSignature(renamed, 'parent')).not.toBe(before)
  })
})

describe('ForkPointMarker', () => {
  const fork = (key: string, direction: ForkChild['direction'] = 'head'): ForkChild => ({ key, title: `Fork ${key}`, direction })

  it('names each fork and opens it on click', () => {
    const onOpen = vi.fn()
    render(<ForkPointMarker forks={[fork('a'), fork('b', 'tail')]} onOpen={onOpen} />)
    expect(screen.getByText('Forked into')).toBeTruthy()
    expect(screen.getByText('Later messages continued in')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Fork b' }))
    expect(onOpen).toHaveBeenCalledWith('b')
    expect(screen.getByRole('button', { name: 'Fork a' }).getAttribute('title')).toBe('Open Fork a')
  })

  it('keeps two forks inline, and past two shows one beside a menu of all of them', async () => {
    const { unmount } = render(<ForkPointMarker forks={[fork('a'), fork('b')]} onOpen={vi.fn()} />)
    expect(screen.getAllByTestId('fork-point-link')).toHaveLength(2)
    expect(screen.queryByTestId('fork-point-show-all')).toBeNull()
    unmount()

    const onOpen = vi.fn()
    render(<ForkPointMarker forks={[fork('a'), fork('b'), fork('c')]} onOpen={onOpen} />)
    // At most two actions in the row: one fork and the menu.
    expect(screen.getAllByTestId('fork-point-link').map(b => b.textContent)).toEqual(['Fork a'])
    fireEvent.pointerDown(screen.getByTestId('fork-point-show-all'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    const items = await screen.findAllByTestId('fork-point-menu-item')
    expect(items.map(i => i.textContent)).toEqual(['Fork a', 'Fork b', 'Fork c'])
    fireEvent.click(items[2])
    expect(onOpen).toHaveBeenCalledWith('c')
  })
})
