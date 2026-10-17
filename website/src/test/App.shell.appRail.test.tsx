/**
 * The rail's Apps-group order model (`shell/nav/appRail.tsx`): the saved order
 * over the merged built-in and installed rows, a drag that reorders and persists
 * it without erasing a hidden app's slot, the haptic taps on pick-up and on a
 * real reorder only, and the hide-time write that makes a hidden app's slot
 * explicit.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core'
import { useAppRailOrder, type AppNavRow } from '../shell/nav/appRail'
import { APP_NAV_HIDDEN_KEY, APP_NAV_ORDER_KEY } from '../lib/appNavHidden'

const haptic = vi.hoisted(() => vi.fn())
vi.mock('../lib/haptic', () => ({ haptic }))

const row = (name: string): AppNavRow => ({ path: `/apps/${name}`, id: `app-${name}`, label: name, group: 'Apps', icon: <span /> })
const APPS = [row('a'), row('b'), row('c')]
const appIds = (ids: string[]) => ids.filter(id => id.startsWith('app-'))
const start = (id: string) => ({ active: { id } }) as unknown as DragStartEvent
const end = (id: string, over: string | null) => ({ active: { id }, over: over ? { id: over } : null }) as unknown as DragEndEvent

describe('useAppRailOrder', () => {
  beforeEach(() => {
    localStorage.clear()
    haptic.mockReset()
  })

  it('lists the installed rows in the saved order', () => {
    localStorage.setItem(APP_NAV_ORDER_KEY, JSON.stringify(['app-c', 'app-a', 'app-b']))
    const { result } = renderHook(() => useAppRailOrder(APPS))
    expect(appIds(result.current.sortedAppGroup.map(n => n.id))).toEqual(['app-c', 'app-a', 'app-b'])
  })

  it('a drag marks the pick-up, and a drop on another row reorders, persists and taps once more', () => {
    const { result } = renderHook(() => useAppRailOrder(APPS))
    act(() => result.current.handleAppDragStart(start('app-a')))
    expect(result.current.activeAppDragId).toBe('app-a')
    expect(haptic).toHaveBeenLastCalledWith('medium')
    act(() => result.current.handleAppDragEnd(end('app-a', 'app-c')))
    expect(result.current.activeAppDragId).toBeNull()
    expect(haptic).toHaveBeenLastCalledWith('light')
    expect(appIds(result.current.sortedAppGroup.map(n => n.id))).toEqual(['app-b', 'app-c', 'app-a'])
    expect(appIds(JSON.parse(localStorage.getItem(APP_NAV_ORDER_KEY) || '[]'))).toEqual(['app-b', 'app-c', 'app-a'])
  })

  it('a drop on itself, on nothing, or a cancel changes nothing and does not tap', () => {
    const { result } = renderHook(() => useAppRailOrder(APPS))
    act(() => result.current.handleAppDragEnd(end('app-a', 'app-a')))
    act(() => result.current.handleAppDragEnd(end('app-a', null)))
    expect(haptic).not.toHaveBeenCalled()
    act(() => result.current.handleAppDragStart(start('app-b')))
    haptic.mockReset()
    act(() => result.current.handleAppDragCancel())
    expect(result.current.activeAppDragId).toBeNull()
    expect(haptic).not.toHaveBeenCalled()
    expect(localStorage.getItem(APP_NAV_ORDER_KEY)).toBeNull()
  })

  it('writes the effective order the moment an app is hidden, and leaves the hidden row out of the list', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-b']))
    const { result } = renderHook(() => useAppRailOrder(APPS))
    expect(appIds(result.current.sortedAppGroup.map(n => n.id))).toEqual(['app-a', 'app-c'])
    expect(appIds(JSON.parse(localStorage.getItem(APP_NAV_ORDER_KEY) || '[]'))).toEqual(['app-a', 'app-b', 'app-c'])
  })
})
