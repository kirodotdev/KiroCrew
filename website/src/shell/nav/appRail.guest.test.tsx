// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'
import { useAppRailOrder } from './appRail'

// `known` is whether /api/auth/me has answered; the owner's pages show only once it said "not a guest".
const guest = vi.hoisted(() => ({ value: true, known: true }))
vi.mock('../../hooks/sharedCrew/useSharedCrew', () => ({
  useIsGuest: () => guest.value,
  useShowsOwnerPages: () => guest.known && !guest.value,
}))

afterEach(() => { cleanup(); guest.value = true; guest.known = true })

const OWNER_PAGES = ['apps', 'capabilities', 'webhooks', 'settings']

describe('the rail for a Shared Crew guest', () => {
  it('hides the owner-only pages', () => {
    const { result } = renderHook(() => useAppRailOrder([]))
    const ids = result.current.advertisedNavItems.map(n => n.id)
    for (const id of OWNER_PAGES) expect(ids).not.toContain(id)
    expect(ids).toContain('chat')
  })

  it('hides them until the identity read has answered, then shows them to the owner', () => {
    guest.value = false
    guest.known = false
    const pending = renderHook(() => useAppRailOrder([]))
    const pendingIds = pending.result.current.advertisedNavItems.map(n => n.id)
    for (const id of OWNER_PAGES) expect(pendingIds).not.toContain(id)
    pending.unmount()
    guest.known = true
    const answered = renderHook(() => useAppRailOrder([]))
    expect(answered.result.current.advertisedNavItems.length).toBeGreaterThan(pendingIds.length)
  })

  it('shows them to the owner', () => {
    guest.value = false
    const { result } = renderHook(() => useAppRailOrder([]))
    const ids = result.current.advertisedNavItems.map(n => n.id)
    expect(ids).toEqual(expect.arrayContaining(['chat']))
  })

  it('shows no installed app rows to a guest', () => {
    const row = { path: '/papyrus', id: 'papyrus', label: 'Papyrus', group: 'Apps', icon: <span />, appName: 'papyrus' }
    const { result } = renderHook(() => useAppRailOrder([row]))
    expect(result.current.sortedAppGroup.map(n => n.id)).not.toContain('papyrus')
  })
})
