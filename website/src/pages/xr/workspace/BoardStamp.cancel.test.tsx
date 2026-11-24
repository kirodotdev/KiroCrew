import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BoardStamp } from './BoardStamp'
import { pinchRoute } from './pinchRoute'

// The stamp's own handlers run as they are, on the element React made for its group; each is handed an event with
// the fields the room's pointer library gives one (`pointerId`, `object`, `stopPropagation`). The frame loop is stood
// in for; the session's store is the one the Crew's units use.
vi.mock('@react-three/fiber', () => ({ useFrame: () => undefined, useThree: () => undefined }))
const xr = vi.hoisted(() => {
  type State = { visibilityState?: string }
  let state: State = { visibilityState: 'visible' }
  const subs = new Set<(s: State, prev: State) => void>()
  return {
    getState: () => state,
    subscribe: (fn: (s: State, prev: State) => void) => { subs.add(fn); return () => { subs.delete(fn) } },
    set: (visibilityState: string | undefined) => { const prev = state; state = { visibilityState }; for (const fn of [...subs]) fn(state, prev) },
    reset: () => { subs.clear(); state = { visibilityState: 'visible' } },
  }
})
vi.mock('@react-three/xr', () => ({ useXRStore: () => xr }))

const propsOf = (el: Element): Record<string, (e: unknown) => void> => {
  const key = Object.keys(el).find(k => k.startsWith('__reactProps$'))
  return key ? ((el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key] ?? {}) : {}
}
/** A pointer event as the library hands it to a handler. */
const ev = (pointerId: number) => ({ pointerId, pointerType: 'ray', object: { setPointerCapture: () => undefined }, stopPropagation: () => undefined, nativeEvent: { clientX: 0, clientY: 0, timeStamp: 0 } })

function stage() {
  const onPress = vi.fn()
  const { container } = render(<BoardStamp board={() => null} widthM={0.3} heightM={0.4} faceM={[0.05, 0.03]} paperRightM={0.15} onPress={onPress} />)
  const el = [...container.querySelectorAll('group')].find(g => (propsOf(g) as unknown as { name?: string }).name === 'board-stamp')
  if (!el) throw new Error('no stamp was drawn')
  const h = propsOf(el)
  return { onPress, down: (id: number) => act(() => { h.onPointerDown(ev(id)) }), up: (id: number) => act(() => { h.onPointerUp(ev(id)) }), cancel: (id: number) => act(() => { h.onPointerCancel(ev(id)) }) }
}

describe('the stamp\'s press that ends by a cancel (D192, 15:50; DASH-17)', () => {
  beforeEach(() => { xr.reset(); vi.spyOn(console, 'error').mockImplementation(() => undefined) })
  afterEach(() => { vi.restoreAllMocks(); pinchRoute.ending = null })

  it('must not lose: his long press, let go (a cancel inside his release), stamps', () => {
    const s = stage()
    s.down(5)
    act(() => { pinchRoute.release('long', () => s.cancel(5)) })
    expect(s.onPress).toHaveBeenCalledTimes(1)
  })
  it('must not act: a long press whose hand is lost (a cancel outside any release) does not stamp, nor does a later up', () => {
    const s = stage()
    s.down(5)
    s.cancel(5)
    s.up(5)
    expect(s.onPress).not.toHaveBeenCalled()
  })
  it('must not act: another pointer\'s cancel ends nothing; his own up then stamps once', () => {
    const s = stage()
    s.down(5)
    act(() => { pinchRoute.release('long', () => s.cancel(6)) })
    expect(s.onPress).not.toHaveBeenCalled()
    s.up(5)
    expect(s.onPress).toHaveBeenCalledTimes(1)
  })
  it('must not act: a hidden session ends the press unpressed; his release after it stamps nothing', () => {
    const s = stage()
    s.down(5)
    act(() => xr.set('hidden'))
    act(() => xr.set('visible'))
    s.up(5)
    act(() => { pinchRoute.release('long', () => s.cancel(5)) })
    expect(s.onPress).not.toHaveBeenCalled()
  })
  it('must not lose: after a hidden session a new press stamps', () => {
    const s = stage()
    act(() => xr.set('hidden'))
    act(() => xr.set('visible'))
    s.down(5)
    s.up(5)
    expect(s.onPress).toHaveBeenCalledTimes(1)
  })
})
