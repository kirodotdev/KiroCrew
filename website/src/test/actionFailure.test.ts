import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  reportActionFailure,
  clearActionFailure,
  useActionFailure,
  __resetActionFailureForTests,
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

  it('keeps the newest rejection when two land', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('First') })
    act(() => { reportActionFailure('Second') })
    expect(result.current.failure?.message).toBe('Second')
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

  it('a success clears the failure reported under its own key', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('The mode change was undone.', 'A', undefined, undefined, { actionKey: 'mode:chat-a' }) })
    act(() => { clearActionFailure('mode:chat-a') })
    expect(result.current.failure).toBeNull()
  })

  it('a success under another key leaves the failure up', () => {
    // The pitfall a bare clear-on-success would have: a pin that lands wiping a
    // fork failure the reader has not read yet.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('No new session was created.', 'A', undefined, undefined, { actionKey: 'fork:chat-a' }) })
    const shown = result.current.failure
    act(() => { clearActionFailure('pin:chat-a') })
    expect(result.current.failure).toBe(shown)
    // Same action, different session: the banner is still true of the first.
    act(() => { clearActionFailure('fork:chat-b') })
    expect(result.current.failure).toBe(shown)
  })

  it('a keyed clear never takes down a failure that named no key', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Session reload failed.') })
    act(() => { clearActionFailure('reload:chat-a') })
    expect(result.current.failure?.message).toBe('Session reload failed.')
  })

  it('the bare clear is the dismiss button and drops a keyed failure too', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('The move was undone.', 'A', undefined, undefined, { actionKey: 'move:chat-a' }) })
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('a keyed clear that matches nothing notifies nobody', () => {
    let renders = 0
    const { result } = renderHook(() => { renders += 1; return useActionFailure() })
    act(() => { reportActionFailure('Boom', 'A', undefined, undefined, { actionKey: 'mode:chat-a' }) })
    const before = renders
    act(() => { clearActionFailure('mode:chat-b') })
    expect(renders).toBe(before)
    expect(result.current.failure?.actionKeys).toEqual(['mode:chat-a'])
  })

  // A failure is keyed per (action, slot), never per set: a bulk revert names one
  // key per session, and the first named slot whose write lands takes the banner
  // down — whatever else landed with it.
  it('a failure over several sessions goes down on any one of their keys', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('The pin change was undone for A and B.', 'A and B', undefined, undefined, { actionKey: ['pin:chat-a', 'pin:chat-b'] }) })
    expect(result.current.failure?.actionKeys).toEqual(['pin:chat-a', 'pin:chat-b'])
    act(() => { clearActionFailure('pin:chat-b') })
    expect(result.current.failure).toBeNull()
  })

  it('a key outside the named set leaves a several-session failure up', () => {
    // Same action on a session the banner does not name, and another action on
    // one it does: neither is the retry the banner is waiting on.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('The pin change was undone for A and B.', 'A and B', undefined, undefined, { actionKey: ['pin:chat-a', 'pin:chat-b'] }) })
    const shown = result.current.failure
    act(() => { clearActionFailure('pin:chat-c') })
    expect(result.current.failure).toBe(shown)
    act(() => { clearActionFailure('mode:chat-a') })
    expect(result.current.failure).toBe(shown)
  })

  it('a report that names no key carries an empty key list, not a missing one', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Session reload failed.') })
    expect(result.current.failure?.actionKeys).toEqual([])
  })

  it('the same keys in a different order are the same report', () => {
    // Identity is on the joined list; a reporter that walks its sessions in a
    // different order has not changed what the reader sees.
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Undone.', 'A and B', undefined, undefined, { actionKey: ['pin:chat-a', 'pin:chat-b'] }) })
    const first = result.current.failure
    act(() => { reportActionFailure('Undone.', 'A and B', undefined, undefined, { actionKey: ['pin:chat-a', 'pin:chat-b'] }) })
    expect(result.current.failure).toBe(first)
    act(() => { reportActionFailure('Undone.', 'A and B', undefined, undefined, { actionKey: ['pin:chat-b', 'pin:chat-a'] }) })
    expect(result.current.failure).not.toBe(first)
  })
})
