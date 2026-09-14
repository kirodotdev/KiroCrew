import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  reportActionFailure,
  clearActionFailure,
  useActionFailure,
  __resetActionFailureForTests,
  MAX_PENDING_FAILURES,
} from '../utils/actionFailure'

describe('the shared surface a rejected gateway write reports to', () => {
  beforeEach(() => { __resetActionFailureForTests() })

  it('carries a rejected write to a live subscriber', () => {
    const { result } = renderHook(() => useActionFailure())
    expect(result.current.failure).toBeNull()
    act(() => { reportActionFailure('Session reload failed.') })
    expect(result.current.failure?.message).toBe('Session reload failed.')
  })

  it('survives the component that observed the rejection', () => {
    // The menu subtree that raised it unmounts as the menu closes, which is why
    // the store outlives it instead of holding the failure in that component.
    act(() => { reportActionFailure('Peer refused the transfer') })
    const { result } = renderHook(() => useActionFailure())
    expect(result.current.failure?.message).toBe('Peer refused the transfer')
  })

  it('clears on dismiss', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Boom') })
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('ignores an empty message, which would render an empty notice', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('') })
    expect(result.current.failure).toBeNull()
  })

  it('keeps the newest rejection on top when two land', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('First') })
    act(() => { reportActionFailure('Second') })
    expect(result.current.failure?.message).toBe('Second')
  })

  it('dismissing the newest rejection brings back the one it covered', () => {
    // A color rejection followed by a move rejection before the reader looked
    // must not leave the color write silently undone.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('The color change was undone.', 'Research notes') })
    const first = result.current.failure
    act(() => { reportActionFailure('The move was undone.', 'Trip planning') })
    act(() => { result.current.clear() })
    expect(result.current.failure).toBe(first)
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('counts the rejections waiting under the shown one', () => {
    const { result } = renderHook(() => useActionFailure())
    expect(result.current.earlier).toBe(0)
    act(() => { reportActionFailure('First') })
    expect(result.current.earlier).toBe(0)
    act(() => { reportActionFailure('Second') })
    act(() => { reportActionFailure('Third') })
    expect(result.current.earlier).toBe(2)
    act(() => { result.current.clear() })
    expect(result.current.earlier).toBe(1)
    act(() => { result.current.clear() })
    expect(result.current.earlier).toBe(0)
  })

  it('a repeat of the shown rejection does not stack a second copy', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('First') })
    act(() => { reportActionFailure('Second') })
    act(() => { reportActionFailure('Second') })
    act(() => { result.current.clear() })
    expect(result.current.failure?.message).toBe('First')
  })

  it('a burst past the bound drops the oldest, not the newest', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => {
      for (let i = 0; i <= MAX_PENDING_FAILURES; i += 1) reportActionFailure(`Failure ${i}`)
    })
    expect(result.current.failure?.message).toBe(`Failure ${MAX_PENDING_FAILURES}`)
    act(() => { for (let i = 1; i < MAX_PENDING_FAILURES; i += 1) result.current.clear() })
    expect(result.current.failure?.message).toBe('Failure 1')
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('a reset empties the covered rejections too', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('First') })
    act(() => { reportActionFailure('Second') })
    act(() => { __resetActionFailureForTests() })
    act(() => { reportActionFailure('Third') })
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('a reset taken after mount leaves the mounted subscriber live', () => {
    // The helper once did `listeners.clear()`, which dropped the subscription
    // `useSyncExternalStore` registered at mount; `subscribe` is module-stable, so
    // React never re-subscribed and a report after the reset reached nobody until
    // some unrelated render happened to re-read the snapshot. The App-level tests
    // reset AFTER `render`, so they have to see the store's own notification: no
    // other re-render trigger is allowed here.
    const { result } = renderHook(() => useActionFailure())
    __resetActionFailureForTests()
    act(() => { reportActionFailure('Session reload failed.') })
    expect(result.current.failure?.message).toBe('Session reload failed.')
  })

  it('clearing when nothing is set notifies nobody', () => {
    let renders = 0
    renderHook(() => { renders += 1; return useActionFailure() })
    const before = renders
    act(() => { clearActionFailure() })
    expect(renders).toBe(before)
  })

  it('a repeated identical rejection keeps the snapshot identity', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Pin change was undone.', 'Research notes') })
    const first = result.current.failure
    act(() => { reportActionFailure('Pin change was undone.', 'Research notes') })
    expect(result.current.failure).toBe(first)
  })

  it('a different subject on the same message still replaces the snapshot', () => {
    // Negative control for the bailout above.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Pin change was undone.', 'Research notes') })
    const first = result.current.failure
    act(() => { reportActionFailure('Pin change was undone.', 'Trip planning') })
    expect(result.current.failure).not.toBe(first)
    expect(result.current.failure?.subject).toBe('Trip planning')
  })

  it('an omitted subject and an empty one are the same state', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Reload failed.') })
    const first = result.current.failure
    act(() => { reportActionFailure('Reload failed.', '') })
    expect(result.current.failure).toBe(first)
  })

  it('carries a per-action heading alongside the subject it names', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('No new session was created.', 'Research notes', undefined, 'Couldn’t duplicate “Research notes”') })
    expect(result.current.failure?.heading).toBe('Couldn’t duplicate “Research notes”')
    expect(result.current.failure?.subject).toBe('Research notes')
  })

  it('drops the heading with the subject it would have named', () => {
    // An unnamed session gets the bare sentence, exactly as on the shared lead.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('No new session was created.', '', undefined, 'Couldn’t duplicate “”') })
    expect(result.current.failure?.heading).toBeUndefined()
    expect(result.current.failure?.subject).toBeUndefined()
  })

  it('a different heading on the same message and subject replaces the snapshot', () => {
    // Negative control for the identity bailout: the heading is part of what the
    // reader sees, so it is part of what makes two reports the same.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Nothing changed.', 'Research notes', undefined, 'Couldn’t send a copy of “Research notes”') })
    const first = result.current.failure
    act(() => { reportActionFailure('Nothing changed.', 'Research notes', undefined, 'Couldn’t duplicate “Research notes”') })
    expect(result.current.failure).not.toBe(first)
    expect(result.current.failure?.heading).toBe('Couldn’t duplicate “Research notes”')
  })

})
