/* #19958 — closing an ADOPTED remote-crew row removes only THIS machine's local
 * wrapper. The session keeps running on its peer, so the peer still lists it and
 * it STAYS LISTED under that peer's group in the sidebar — that is the honest,
 * reachable "way back" the row's hint names.
 *
 * The earlier revision tried to HIDE the re-surfacing peer row with a local
 * suppression set; First Principles and GPT 6.1 showed that hid the very row the
 * hint points to (and erased the dismissal while the peer list loaded), so the
 * suppression was deleted (lead ruling, 2026-10-15). This test pins the real
 * path that remains:
 *   1. removing an adopted row dispatches only the LOCAL close — no peer stop
 *      request is ever sent (the hub has no cross-link close); and
 *   2. the peer's own row stays listed under its crew group with no local wrapper,
 *      so the session the hint sends the user to is actually there.
 */
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
// A minimal api mock. `deleteChatSlot` is the LOCAL close deleteSlot calls.
// Deliberately NO peer-stop / crew-stop method exists here: if deleteSlot ever
// grew one for an adopted row, this test's `api` would be missing it and the
// thunk would throw — the test would stop being green, which is the alarm we want.
vi.mock('../api/client', () => ({
  api: {
    deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
    chatSlotDetail: vi.fn(() => Promise.resolve({ messages: [], has_more: false, total: 0, next_before: 0, running: false })),
    chatSlots: vi.fn(() => Promise.resolve([])),
  },
}))

import dashboardReducer, { sseSlots } from './dashboardSlice'
import chatReducer, { deleteSlot } from './chatSlice'
import { crewGroupsFor } from '../hooks/useInstanceSessions'
import { sessionRowIdentity } from '../pages/chat-sidebar/rowIdentity'
import type { Slot } from '../pages/chat-sidebar/types'
import type { InstanceView } from '../api/client'
import { api } from '../api/client'

const deleteChatSlot = vi.mocked(api.deleteChatSlot)

const makeStore = () => configureStore({
  reducer: { chat: chatReducer, dashboard: dashboardReducer },
  middleware: g => g({ serializableCheck: false, immutableCheck: false }),
})

beforeEach(() => { deleteChatSlot.mockClear() })

describe('#19958 release-locally — the honest remaining path', () => {
  it('removing an adopted row sends only the LOCAL close, never a peer stop request', async () => {
    const store = makeStore()
    // An ADOPTED remote-crew row: a LOCAL slot (no peer_id) whose turns execute on
    // a peer (executor: 'remote' + instance_id), resolved to the peer's identity.
    store.dispatch(sseSlots([{
      key: 'local-wrapper', title: 'Adopted', messages: 3, running: false,
      executor: 'remote', instance_id: 'inst-a', row_identity: 'inst-a:chat-9',
    } as unknown as Parameters<typeof sseSlots>[0][number]]))

    await store.dispatch(deleteSlot({ key: 'local-wrapper' }))

    // The ONE call is the local wrapper close. No peer stop API is defined on the
    // mock, so any attempt to reach the peer session would have thrown above.
    expect(deleteChatSlot).toHaveBeenCalledTimes(1)
    expect(deleteChatSlot).toHaveBeenCalledWith(expect.objectContaining({ key: 'local-wrapper' }))
  })

  it('a peer session with NO local wrapper stays listed under its crew group', () => {
    // After the local wrapper is removed, the peer still owns and lists the
    // session; it renders as a peer row filed under the peer's crew group. Nothing
    // filters it out of the merged list — the hint's "stays listed under {peer}"
    // is literally what the sidebar does.
    const connected = { id: 'inst-a', name: 'nobita', connection: { state: 'connected' } } as unknown as InstanceView
    const peerRow = { key: 'chat-9', peer_id: 'inst-a', peer_name: 'nobita', row_identity: 'inst-a:chat-9' } as unknown as Slot

    const groups = crewGroupsFor([connected], [peerRow])
    expect(groups.map(g => g.id)).toContain('inst-a')

    // And the peer row is NOT suppressed out of the merged list: with no local
    // wrapper and no suppression set, a plain merge keeps it.
    const local: Slot[] = []
    const seen = new Set(local.map(sessionRowIdentity))
    const merged = [...local, ...[peerRow].filter(r => !seen.has(sessionRowIdentity(r)))]
    expect(merged.map(sessionRowIdentity)).toContain('inst-a:chat-9')
  })
})
