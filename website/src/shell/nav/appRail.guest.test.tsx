// @vitest-environment jsdom
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useGuestIdentityError, useIsGuest } from '../../hooks/sharedCrew/useSharedCrew'
import { GUEST_HIDDEN_NAV_IDS } from '../../lib/ownerOnly'
import { useAppRailOrder } from './appRail'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

/** The rail as the real identity read leaves it, with `/api/auth/me` answering via *answer*. */
function renderRail(answer: () => Promise<boolean>, rows: Parameters<typeof useAppRailOrder>[0] = []) {
  vi.spyOn(api, 'sharedCrewIsGuest').mockImplementation(answer)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children)
  return renderHook(() => ({ rail: useAppRailOrder(rows), guest: useIsGuest(), error: useGuestIdentityError() }), { wrapper })
}

const navIds = (view: ReturnType<typeof renderRail>) => view.result.current.rail.advertisedNavItems.map(n => n.id)

describe('the rail for a Shared Crew guest', () => {
  it('hides the owner-only pages once the server says this tab is a guest', async () => {
    const view = renderRail(() => Promise.resolve(true))
    await waitFor(() => expect(view.result.current.guest).toBe(true))
    const ids = navIds(view)
    for (const id of GUEST_HIDDEN_NAV_IDS) expect(ids).not.toContain(id)
    expect(ids).toContain('chat')
  })

  it("shows the owner's pages while the identity read is pending or after it failed", async () => {
    const owner = renderRail(() => Promise.resolve(false))
    await waitFor(() => expect(api.sharedCrewIsGuest).toHaveBeenCalled())
    const ownerIds = navIds(owner)
    expect(ownerIds.some(id => GUEST_HIDDEN_NAV_IDS.has(id))).toBe(true)
    owner.unmount()

    const pending = renderRail(() => new Promise<boolean>(() => {}))
    expect(navIds(pending)).toEqual(ownerIds)
    pending.unmount()

    const failed = renderRail(() => Promise.reject(new Error('identity read failed')))
    await waitFor(() => expect(failed.result.current.error).toBe('identity read failed'))
    expect(navIds(failed)).toEqual(ownerIds)
  })

  it('shows no installed app rows to a guest', async () => {
    const row = { path: '/papyrus', id: 'papyrus', label: 'Papyrus', group: 'Apps', icon: <span />, appName: 'papyrus' }
    const view = renderRail(() => Promise.resolve(true), [row])
    await waitFor(() => expect(view.result.current.guest).toBe(true))
    expect(view.result.current.rail.sortedAppGroup.map(n => n.id)).not.toContain('papyrus')
  })
})
