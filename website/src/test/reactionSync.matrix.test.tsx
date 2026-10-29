/**
 * The reaction sync contract, run as a matrix.
 *
 * Invariants:
 *   I1 every QueryClient converges to the server's state after a toggle, a
 *      cross-tab `reactions_changed` frame or a reconnect;
 *   I2 a pending toggle on one client never suppresses another client's
 *      refresh;
 *   I3 a failed toggle surfaces on the mount that shows its conversation, and
 *      only there (not on other clients, not on another conversation's mount);
 *   I4 nothing double-applies: one intended write sends one request, and no
 *      cache holds a duplicate record.
 *
 * Topology: tab A holds the dashboard client and two app clients (an embedded
 * app chat runs on its own QueryClient); tab B is a second document with its
 * own dashboard client. Every scenario runs with each tab-A client as the
 * actor. The frame and reconnect paths are driven through the same ledger
 * entry points `useWebSocket` and the reconnect handler call, with the client
 * list each tab would pass (its dashboard client plus its registered app
 * clients).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useChatReactions } from '../hooks/useChatReactions'
import {
  invalidateReactionsForSlotOn,
  reconcileReactionsOnReconnect,
  __resetReactionToggleLedgerForTests,
} from '../hooks/reactionToggleLedger'
import type { ChatReaction, ReactMessageBody } from '../api/reactions'

vi.mock('../api/reactions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/reactions')>()
  return {
    REACTION_EMOJI: actual.REACTION_EMOJI,
    reactionsApi: { list: vi.fn(), create: vi.fn(), remove: vi.fn() },
  }
})
import { reactionsApi } from '../api/reactions'

const SLOT = 'dashboard:chat-1'
const OTHER_SLOT = 'dashboard:chat-2'
const MID = 'm-1'

// ── An in-memory server with the routes' real semantics ────────────────────
let server: ChatReaction[] = []
let cap = 500
let nextId = 0
let gate: Promise<void> | null = null
let openGate: () => void = () => {}
let failNextRemove = false

function holdWrites() {
  gate = new Promise<void>(res => { openGate = () => { gate = null; res() } })
}
const apiError = (status: number, body = '') => Object.assign(new Error(`HTTP ${status}`), { status, body })

function installServer() {
  ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockImplementation(async (slot: string) => ({
    reactions: server.filter(r => r.slot_key === slot).map(r => ({ ...r })),
  }))
  ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation(async (body: ReactMessageBody) => {
    if (gate) await gate
    const existing = server.find(r => r.slot_key === body.slot_key && r.mid === body.mid && r.emoji === body.emoji)
    if (existing) return { ...existing }
    if (server.filter(r => r.slot_key === body.slot_key).length >= cap) {
      throw apiError(409, JSON.stringify({ code: 'reaction_limit_reached', limit: cap }))
    }
    const record: ChatReaction = { id: `r-${++nextId}`, ...body, reacted_at: '2026-10-25T00:00:00Z' }
    server.push(record)
    return { ...record }
  })
  ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockImplementation(async (slot: string, mid: string, emoji: string) => {
    if (gate) await gate
    if (failNextRemove) { failNextRemove = false; throw apiError(500) }
    const i = server.findIndex(r => r.slot_key === slot && r.mid === mid && r.emoji === emoji)
    if (i < 0) throw apiError(404)
    server.splice(i, 1)
    return { ok: true }
  })
}

function seed(emoji: string, mid = MID, slot = SLOT) {
  server.push({ id: `r-${++nextId}`, slot_key: slot, mid, emoji, reacted_at: '2026-10-25T00:00:00Z' })
}
const serverSet = (mid = MID, slot = SLOT) =>
  server.filter(r => r.slot_key === slot && r.mid === mid).map(r => r.emoji).sort()

// ── Topology ───────────────────────────────────────────────────────────────
const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
type Mount = ReturnType<typeof mountOn>
function mountOn(qc: QueryClient, slot = SLOT) {
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children)
  return renderHook(({ s }: { s: string }) => useChatReactions(s), { wrapper, initialProps: { s: slot } })
}

interface World {
  tabA: QueryClient[] // [dashboard, app1, app2]
  tabB: QueryClient[] // [dashboard]
  mounts: Map<QueryClient, Mount>
}
function buildWorld(): World {
  const tabA = [newClient(), newClient(), newClient()]
  const tabB = [newClient()]
  const mounts = new Map<QueryClient, Mount>()
  for (const qc of [...tabA, ...tabB]) mounts.set(qc, mountOn(qc))
  return { tabA, tabB, mounts }
}
/** The server broadcasts `reactions_changed` to every tab after a write;
 *  each tab routes it to its own dashboard client plus its app clients. */
function broadcast(w: World, slot = SLOT) {
  invalidateReactionsForSlotOn(w.tabA, slot)
  invalidateReactionsForSlotOn(w.tabB, slot)
}
const shown = (m: Mount, mid = MID) => [...m.result.current.reactionsFor(mid)].sort()
async function expectAllConverged(w: World) {
  for (const m of w.mounts.values()) {
    await waitFor(() => expect(shown(m)).toEqual(serverSet()))
  }
}
function expectNoDuplicates(w: World) {
  for (const qc of [...w.tabA, ...w.tabB]) {
    const rows = qc.getQueryData<ChatReaction[]>(['chat-reactions', SLOT]) ?? []
    const identities = rows.map(r => `${r.mid}|${r.emoji}`)
    expect(new Set(identities).size).toBe(identities.length)
  }
}
async function loaded(w: World) {
  for (const m of w.mounts.values()) await waitFor(() => expect(m.result.current.loading).toBe(false))
}

const ACTORS = [
  { name: 'dashboard client', index: 0 },
  { name: 'app client 1', index: 1 },
  { name: 'app client 2', index: 2 },
] as const

beforeEach(() => {
  vi.clearAllMocks()
  __resetReactionToggleLedgerForTests()
  server = []
  cap = 500
  nextId = 0
  gate = null
  failNextRemove = false
  installServer()
})

describe.each(ACTORS)('reaction sync contract, actor = $name', ({ index }) => {
  it('I1/I4: a settled toggle converges every client in both tabs, with one request', async () => {
    const w = buildWorld()
    await loaded(w)
    const actor = w.mounts.get(w.tabA[index])!
    await act(async () => { await actor.result.current.toggleReaction(MID, '👍') })
    act(() => broadcast(w))

    expect(serverSet()).toEqual(['👍'])
    await expectAllConverged(w)
    expect(reactionsApi.create).toHaveBeenCalledTimes(1)
    expectNoDuplicates(w)
  })

  it('I2: a frame during the actor\'s pending toggle refreshes every other client now, the actor on drain', async () => {
    const w = buildWorld()
    await loaded(w)
    const actorQc = w.tabA[index]
    const actor = w.mounts.get(actorQc)!
    holdWrites()
    let toggle: Promise<void> = Promise.resolve()
    act(() => { toggle = actor.result.current.toggleReaction(MID, '👍') })
    await waitFor(() => expect(reactionsApi.create).toHaveBeenCalledTimes(1))

    seed('⭐') // another tab's write lands on the server
    act(() => broadcast(w))
    // Every client but the actor refreshes at once and shows ⭐.
    for (const [qc, m] of w.mounts) {
      if (qc === actorQc) continue
      await waitFor(() => expect(shown(m)).toEqual(['⭐']))
    }
    // The actor holds its refresh (its optimistic 👍 is still the only row).
    expect(shown(actor)).toEqual(['👍'])

    await act(async () => { openGate(); await toggle })
    act(() => broadcast(w)) // the actor's own write is broadcast too
    expect(serverSet()).toEqual(['⭐', '👍'])
    await expectAllConverged(w)
    expectNoDuplicates(w)
  })

  it('I1/I2: a reconnect mid-toggle converges every client, deferring only the actor', async () => {
    const w = buildWorld()
    await loaded(w)
    const actorQc = w.tabA[index]
    const actor = w.mounts.get(actorQc)!
    holdWrites()
    let toggle: Promise<void> = Promise.resolve()
    act(() => { toggle = actor.result.current.toggleReaction(MID, '👍') })
    await waitFor(() => expect(reactionsApi.create).toHaveBeenCalledTimes(1))

    seed('👀') // a frame for this write was missed while the socket was down
    act(() => {
      reconcileReactionsOnReconnect(w.tabA)
      reconcileReactionsOnReconnect(w.tabB)
    })
    for (const [qc, m] of w.mounts) {
      if (qc === actorQc) continue
      await waitFor(() => expect(shown(m)).toEqual(['👀']))
    }
    await act(async () => { openGate(); await toggle })
    act(() => broadcast(w))
    await expectAllConverged(w)
    expectNoDuplicates(w)
  })

  it('I3: a remove that fails after the actor unmounted shows on its next mount only', async () => {
    seed('👍')
    const w = buildWorld()
    await loaded(w)
    const actorQc = w.tabA[index]
    holdWrites()
    failNextRemove = true
    let toggle: Promise<void> = Promise.resolve()
    act(() => { toggle = w.mounts.get(actorQc)!.result.current.toggleReaction(MID, '👍') })
    await waitFor(() => expect(reactionsApi.remove).toHaveBeenCalledTimes(1))
    w.mounts.get(actorQc)!.unmount()
    const remount = mountOn(actorQc)
    w.mounts.set(actorQc, remount)

    await act(async () => { openGate(); await toggle.catch(() => {}) })
    expect(remount.result.current.error).toEqual({ slot: SLOT, mid: MID, emoji: '👍', kind: 'unreact' })
    for (const [qc, m] of w.mounts) {
      if (qc !== actorQc) expect(m.result.current.error).toBeNull()
    }
    // Nothing changed on the server and no frame is sent for a failure.
    expect(serverSet()).toEqual(['👍'])
    await expectAllConverged(w)
  })

  it('I3/I4: a 409 at the cap leaves every cache on the server state and names the cap on the actor only', async () => {
    cap = 1
    seed('⭐', 'm-other')
    const w = buildWorld()
    await loaded(w)
    const actorQc = w.tabA[index]
    await act(async () => { await w.mounts.get(actorQc)!.result.current.toggleReaction(MID, '👍').catch(() => {}) })

    expect(w.mounts.get(actorQc)!.result.current.error).toEqual({ slot: SLOT, mid: MID, emoji: '👍', kind: 'limit', limit: 1 })
    for (const [qc, m] of w.mounts) {
      if (qc !== actorQc) expect(m.result.current.error).toBeNull()
    }
    expect(serverSet()).toEqual([])
    await expectAllConverged(w)
    expect(reactionsApi.create).toHaveBeenCalledTimes(1)
    expectNoDuplicates(w)
  })

  it('I3: a failure in one conversation is not cleared or overwritten by another mount on the same client', async () => {
    // The main chat and the companion panel share one client. A failure on
    // the main chat's slot must survive a toggle, and a failure, in the panel.
    const w = buildWorld()
    await loaded(w)
    const qc = w.tabA[index]
    const main = w.mounts.get(qc)!
    const panel = mountOn(qc, OTHER_SLOT)
    await waitFor(() => expect(panel.result.current.loading).toBe(false))

    failNextRemove = true
    seed('👍')
    act(() => broadcast(w))
    await waitFor(() => expect(shown(main)).toEqual(['👍']))
    await act(async () => { await main.result.current.toggleReaction(MID, '👍').catch(() => {}) })
    const mainError = { slot: SLOT, mid: MID, emoji: '👍', kind: 'unreact' }
    expect(main.result.current.error).toEqual(mainError)

    await act(async () => { await panel.result.current.toggleReaction('m-panel', '✅') })
    expect(main.result.current.error).toEqual(mainError)

    cap = 0 // the panel's next add fails at the cap
    await act(async () => { await panel.result.current.toggleReaction('m-panel', '👀').catch(() => {}) })
    expect(panel.result.current.error).toMatchObject({ slot: OTHER_SLOT, kind: 'limit' })
    expect(main.result.current.error).toEqual(mainError)

    act(() => main.result.current.clearError())
    expect(main.result.current.error).toBeNull()
    expect(panel.result.current.error).toMatchObject({ slot: OTHER_SLOT, kind: 'limit' })
  })
})
