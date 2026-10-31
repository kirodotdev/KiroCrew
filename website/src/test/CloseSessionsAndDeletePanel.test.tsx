// The folder ⋮ → "Close sessions and delete…" panel.
//
// Pinned here:
// - the preview totals count sub-folders, the sessions it will close, and how
//   many of those have a turn running
// - a running turn, an armed loop and a pinned chat are each flagged
// - confirm closes exactly the previewed session keys, then removes the
//   folders-to-delete ids with include_top_level
// - an app-owned session, a crew member's chat and the active chat are KEPT,
//   listed as kept, and their folder chain is excluded from the delete
// - a session that fails to close leaves its folder (and its ancestors) out of
//   the delete, and the panel stays open and says so
// - the preview is FROZEN at open: a slot added to the tree afterwards is not
//   closed and not listed
// - a folder with its own setting (or a channel / crewmate folder) is listed as
//   kept, left out of the delete count, and keeps its parent chain
// - a run where the server deleted fewer folders than asked stays open and says
//   so
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import CloseSessionsAndDeletePanel from '../pages/chat-sidebar/CloseSessionsAndDeletePanel'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { api } from '../api/client'
import type { ChatFolder } from '../types'
import type { AgentInfo, Slot } from '../pages/chat-sidebar/types'

// oss (top) / ci (sub) / worker (sub-sub)
const FOLDERS = [
  { id: 'oss', name: 'oss', parent_id: null },
  { id: 'ci', name: 'ci', parent_id: 'oss' },
  { id: 'worker', name: 'worker', parent_id: 'ci' },
] as unknown as ChatFolder[]

const folder = FOLDERS[0]

const AGENTS: AgentInfo[] = [
  { name: 'mate-bob', source: 'member', selection_kind: 'member' } as unknown as AgentInfo,
]

function slot(p: Partial<Slot> & { key: string }): Slot {
  return { running: false, ...p } as Slot
}

function panel(slots: Slot[], folders: ChatFolder[], activeSlot: string | null, onClose: () => void, target: ChatFolder = folder, key?: string) {
  return (
    <Provider store={store}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CloseSessionsAndDeletePanel
          key={key} folder={target} folders={folders} slots={slots}
          activeSlot={activeSlot} agents={AGENTS} loopKeys={new Set()} onClose={onClose}
        />
      </QueryClientProvider>
    </Provider>
  )
}

function mount(
  slots: Slot[], activeSlot: string | null = null, loopKeys = new Set<string>(), onClose = vi.fn(),
  folders: ChatFolder[] = FOLDERS, target: ChatFolder = folder,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const r = render(
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <CloseSessionsAndDeletePanel
          folder={target} folders={folders} slots={slots}
          activeSlot={activeSlot} agents={AGENTS} loopKeys={loopKeys} onClose={onClose}
        />
      </QueryClientProvider>
    </Provider>,
  )
  return { onClose, rerender: r.rerender }
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); store.dispatch(setActiveSlot(null)) })

describe('CloseSessionsAndDeletePanel', () => {
  it('counts sub-folders, sessions to close and running, and flags each kind', () => {
    mount([
      slot({ key: 's1', title: 'alpha', folder_id: 'ci', running: true }),
      slot({ key: 's2', title: 'beta', folder_id: 'worker', pinned: true }),
    ], null, new Set(['s2']))
    // 2 sub-folders (ci, worker), 2 to close, 1 running.
    const totals = screen.getByTestId('close-and-delete-totals').textContent ?? ''
    expect(totals).toContain('2 sub-folders')
    expect(totals).toContain('2 sessions')
    expect(totals).toContain('1 with a turn running')
    // s1 running, s2 armed loop + pinned.
    expect(screen.getAllByTestId('flag-running').length).toBe(1)
    expect(screen.getAllByTestId('flag-loop').length).toBe(1)
    expect(screen.getAllByTestId('flag-pinned').length).toBe(1)
    // The confirm names both counts (all three folders are deletable here).
    expect(screen.getByTestId('close-and-delete-confirm').textContent).toBe('Close 2 sessions and delete 3 folders')
  })

  it('closes exactly the previewed keys, then deletes the folders-to-delete ids', async () => {
    const close = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue({} as never)
    const cleanup = vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: ['oss', 'ci', 'worker'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const { onClose } = mount([
      slot({ key: 's1', title: 'alpha', folder_id: 'ci' }),
      slot({ key: 's2', title: 'beta', folder_id: 'worker' }),
    ])
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(close).toHaveBeenCalledTimes(2))
    expect(close.mock.calls.map(c => c[0]).sort()).toEqual(['s1', 's2'])
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    const arg = cleanup.mock.calls[0][0]
    expect(arg.includeTopLevel).toBe(true)
    expect([...(arg.ids ?? [])].sort()).toEqual(['ci', 'oss', 'worker'])
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('keeps an app-owned session, a crew member chat and the active chat, with their folder chain', () => {
    mount([
      slot({ key: 'app1', title: 'app chat', folder_id: 'worker', app: 'some-app' }),
      slot({ key: 'mate1', title: 'mate chat', folder_id: 'ci', agent_kind: 'member' }),
      slot({ key: 'me', title: 'current', folder_id: 'oss' }),
    ], 'me')
    // All three are kept; none is offered to close.
    expect(screen.getAllByTestId('close-and-delete-kept-row').length).toBe(3)
    expect(screen.queryByTestId('close-and-delete-session')).toBeNull()
    // Their chains (oss, ci, worker) are all kept alive, so nothing is deletable.
    expect(screen.queryByTestId('close-and-delete-folders')).toBeNull()
    expect(screen.getByText('Nothing to close or delete in this folder.')).toBeTruthy()
    const kept = screen.getByTestId('close-and-delete-kept').textContent ?? ''
    expect(kept).toContain('Owned by an app')
    expect(kept).toContain("Crew member's chat")
    expect(kept).toContain('Open now')
  })

  it('a kept session in a deep folder keeps its whole chain out of the delete', () => {
    // A kept session in `worker` keeps oss/ci/worker; a closable one in `ci`
    // would still leave ci (and oss) alive, so only... none are deletable.
    mount([
      slot({ key: 'app1', title: 'app', folder_id: 'worker', app: 'x' }),
      slot({ key: 's1', title: 'alpha', folder_id: 'ci' }),
    ])
    // oss, ci, worker are all on the kept chain of app1, so no folder deletes.
    expect(screen.queryByTestId('close-and-delete-folders')).toBeNull()
    // But s1 is still closable.
    expect(screen.getAllByTestId('close-and-delete-session').length).toBe(1)
    expect(screen.getByTestId('close-and-delete-confirm').textContent).toBe('Close 1 session and delete 0 folders')
  })

  it('stays open and says so when a session fails to close', async () => {
    vi.spyOn(api, 'deleteChatSlot').mockRejectedValue(new Error('still running'))
    const cleanup = vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: [] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const { onClose } = mount([slot({ key: 's1', title: 'alpha', folder_id: 'ci' })])
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    // A genuine close rejection is an error the person can act on — surfaced
    // through ErrorNotice (askAgent), not the muted status notice.
    expect(await screen.findByTestId('close-and-delete-error')).toBeTruthy()
    expect(screen.getByTestId('close-and-delete-error').textContent).toContain('still running')
    expect(screen.queryByTestId('close-and-delete-notice')).toBeNull()
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a 404 close keeps its own folder chain out of cleanup but sends off-chain folders', async () => {
    // A 404 (another tab already closed it, outcome unresolved) keeps that
    // session's folder chain OUT of this run's cleanup -- the session may be
    // mid-restore, so deleting its folder could strand it unfiled. The chain is
    // reported as stayed and the panel stays open; a later run removes it.
    const notFound = Object.assign(new Error('not found'), { status: 404 })
    const close = vi.spyOn(api, 'deleteChatSlot').mockRejectedValue(notFound)
    const cleanup = vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: ['worker'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const { onClose } = mount([slot({ key: 's1', title: 'alpha', folder_id: 'ci' })])
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(close).toHaveBeenCalledWith('s1'))
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    // The 404 session's chain (oss, ci) is excluded; only the off-chain empty
    // worker is sent. A chain was kept -> notice shows, panel stays open.
    const sent = [...(cleanup.mock.calls[0][0].ids ?? [])]
    expect(sent).not.toContain('oss')
    expect(sent).not.toContain('ci')
    expect(sent).toContain('worker')
    expect(await screen.findByTestId('close-and-delete-notice')).toBeTruthy()
    expect(screen.queryByTestId('close-and-delete-error')?.textContent || '').toBe('')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a 404 close keeps its folder chain out of the cleanup ids; other folders go', async () => {
    // Lead F1 fix. oss(top)/ci/worker; a closable session in ci 404s, a
    // closable session in worker closes. The 404 session's chain (oss, ci) is
    // excluded from cleanup; worker (and only worker) is sent; the notice shows.
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => {
      if (key === 's_ci') throw Object.assign(new Error('gone'), { status: 404 })
      return {} as never
    })
    const cleanup = vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: ['worker'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const { onClose } = mount([
      slot({ key: 's_ci', title: 'in ci', folder_id: 'ci' }),
      slot({ key: 's_worker', title: 'in worker', folder_id: 'worker' }),
    ])
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    const sentIds = [...(cleanup.mock.calls[0][0].ids ?? [])]
    // The 404 session's chain (oss, ci) is NOT sent; worker IS.
    expect(sentIds).not.toContain('oss')
    expect(sentIds).not.toContain('ci')
    expect(sentIds).toContain('worker')
    // A chain was kept this run -> the "some folders stayed" notice, panel open.
    expect(await screen.findByTestId('close-and-delete-notice')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the session row wraps so flags drop below the identity at narrow widths', () => {
    mount([slot({ key: 's1', title: 'alpha', folder_id: 'ci', running: true, pinned: true })], null, new Set(['s1']))
    const row = screen.getByTestId('close-and-delete-session')
    // The row wraps and the identity takes the flexible column (min-w-0 flex-1),
    // so the flag cluster drops to the next line instead of squeezing the title.
    expect(row.className).toContain('flex-wrap')
    expect(row.querySelector('.min-w-0.flex-1')).toBeTruthy()
    // The confirm label is allowed to wrap (no whitespace-nowrap).
    expect(screen.getByTestId('close-and-delete-confirm').className).not.toContain('whitespace-nowrap')
  })

  it('keeps Cancel/Confirm out of the scrolling body so they never scroll away', () => {
    // Many items would overflow a short sidebar; the action buttons must sit
    // OUTSIDE the one scrolling body so they stay reachable (#26791 review F2).
    const many: ChatFolder[] = [{ id: 'root', name: 'root', parent_id: null } as unknown as ChatFolder]
    const slots: Slot[] = []
    for (let i = 0; i < 12; i++) {
      many.push({ id: `f${i}`, name: `f${i}`, parent_id: 'root' } as unknown as ChatFolder)
      slots.push(slot({ key: `s${i}`, title: `session ${i}`, folder_id: `f${i}` }))
    }
    mount(slots, null, new Set(), vi.fn(), many, many[0])
    const scroll = screen.getByTestId('close-and-delete-scroll')
    // The one scrolling region carries the overflow class.
    expect(scroll.className).toContain('overflow-y-auto')
    // The lists live inside it; the action buttons do not.
    expect(scroll.contains(screen.getByTestId('close-and-delete-sessions'))).toBe(true)
    expect(scroll.contains(screen.getByTestId('close-and-delete-confirm'))).toBe(false)
    expect(scroll.contains(screen.getByText('Cancel'))).toBe(false)
    // The panel root is height-bounded so the body has something to scroll within.
    expect(screen.getByTestId('close-and-delete-panel').className).toMatch(/max-h-/)
  })

  it('re-reads the active slot each iteration: a session switched into mid-run is skipped', async () => {
    // The person switches into a session AFTER the loop has started: the active
    // key is re-read before each delete, so a session switched into mid-run is
    // still skipped (not only one active before confirm).
    const close = vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => {
      // Simulate the person opening s2 while s1's close is in flight.
      if (key === 's1') store.dispatch(setActiveSlot('s2'))
      return {} as never
    })
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: [] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    mount([
      slot({ key: 's1', title: 'alpha', folder_id: 'ci' }),
      slot({ key: 's2', title: 'beta', folder_id: 'worker' }),
    ], null)
    // Both are previewed as closable (no active slot at open).
    expect(screen.getAllByTestId('close-and-delete-session').length).toBe(2)
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(close).toHaveBeenCalled())
    // s1 closes; s2 became active during s1's close, so it is skipped.
    expect(close.mock.calls.map(c => c[0])).toEqual(['s1'])
    store.dispatch(setActiveSlot(null))
  })

  it('a kind-less crewmate chat is kept when the roster lists it as a member', () => {
    // No agent_kind on the slot, but the roster names its agent as a member.
    // Passing a reload-surviving roster (ChatSidebar's tintAgents) is what keeps
    // this classification stable while the catalog reloads.
    mount(
      [slot({ key: 'm1', title: 'mate work', folder_id: 'ci', agent: 'mate-bob' })],
      null,
    )
    expect(screen.getAllByTestId('close-and-delete-kept-row').length).toBe(1)
    expect(screen.queryByTestId('close-and-delete-session')).toBeNull()
    expect(screen.getByTestId('close-and-delete-kept').textContent).toContain("Crew member's chat")
  })

  it('a retry after partial folder deletion completes instead of staying stuck', async () => {
    // Root with siblings A and B. First run: A's session fails, B's closes and
    // B is deleted. Retry: A closes, A and root are deleted. The retry must NOT
    // count the already-deleted B as "stayed", so the panel closes.
    const FOLDS = [
      { id: 'root', name: 'root', parent_id: null },
      { id: 'A', name: 'A', parent_id: 'root' },
      { id: 'B', name: 'B', parent_id: 'root' },
    ] as unknown as ChatFolder[]
    let aAttempts = 0
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => {
      if (key === 'inA') { aAttempts += 1; if (aAttempts === 1) throw new Error('busy') }
      return {} as never
    })
    let cleanupCall = 0
    const cleanup = vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => {
      cleanupCall += 1
      // First run: only B is empty (A still holds the failed session) -> B deleted.
      // Retry: A now empty -> A and root deleted.
      const deleted = cleanupCall === 1 ? ['B'] : (opts.ids ?? [])
      return { deleted } as Awaited<ReturnType<typeof api.cleanupChatFolders>>
    })
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const onClose = vi.fn()
    mount([
      slot({ key: 'inA', title: 'in A', folder_id: 'A' }),
      slot({ key: 'inB', title: 'in B', folder_id: 'B' }),
    ], null, new Set(), onClose, FOLDS, FOLDS[0])
    // First run: A fails -> error shown, panel stays open.
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(screen.getByTestId('close-and-delete-error')).toBeTruthy())
    expect(onClose).not.toHaveBeenCalled()
    // Retry: only the still-undeleted ids are sent (B is excluded), A closes.
    await waitFor(() => expect((screen.getByTestId('close-and-delete-confirm') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    // Second cleanup call did not include the already-deleted B.
    expect(cleanup.mock.calls[1][0].ids).not.toContain('B')
  })

  it('on retry after one real failure, re-sends only the failed key', async () => {
    // s1 fails once then closes; s2 closes first try. The retry must not
    // re-DELETE s2 (already closed), only s1.
    const calls: string[] = []
    let s1Attempts = 0
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => {
      calls.push(key)
      if (key === 's1') { s1Attempts += 1; if (s1Attempts === 1) throw new Error('busy') }
      return {} as never
    })
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: [] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    mount([
      slot({ key: 's1', title: 'alpha', folder_id: 'ci' }),
      slot({ key: 's2', title: 'beta', folder_id: 'worker' }),
    ])
    // First run: s1 fails, s2 closes.
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(screen.getByTestId('close-and-delete-error')).toBeTruthy())
    expect([...calls].sort()).toEqual(['s1', 's2'])
    // Retry: only the still-open s1 is re-sent; s2 is skipped.
    calls.length = 0
    await waitFor(() => expect((screen.getByTestId('close-and-delete-confirm') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(calls).toEqual(['s1']))
  })

  it('freezes the preview: a slot added after open is neither closed nor listed', async () => {
    const close = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue({} as never)
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: [] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const onClose = vi.fn()
    const { rerender } = mount([slot({ key: 's1', title: 'alpha', folder_id: 'ci' })], null, new Set(), onClose)
    expect(screen.getAllByTestId('close-and-delete-session').length).toBe(1)
    // A new session is filed into the tree while the panel is open.
    rerender(panel(
      [slot({ key: 's1', title: 'alpha', folder_id: 'ci' }), slot({ key: 'late', title: 'late', folder_id: 'worker' })],
      FOLDERS, null, onClose,
    ))
    // The frozen preview still shows only the original one.
    expect(screen.getAllByTestId('close-and-delete-session').length).toBe(1)
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(close).toHaveBeenCalled())
    // 'late' is never closed — only the previewed key is.
    expect(close.mock.calls.map(c => c[0])).toEqual(['s1'])
  })

  it('lists a folder with its own setting as kept, out of the delete count, and keeps its parent', () => {
    const foldersWithColor = [
      { id: 'oss', name: 'oss', parent_id: null },
      { id: 'ci', name: 'ci', parent_id: 'oss', color: '#ff0000' },
      { id: 'worker', name: 'worker', parent_id: 'ci' },
    ] as unknown as ChatFolder[]
    mount([], null, new Set(), vi.fn(), foldersWithColor)
    // ci has a color -> kept; its parent oss stays out; worker (empty, below the
    // kept ci) is the only deletable folder.
    const keptFolders = screen.getByTestId('close-and-delete-kept-folders').textContent ?? ''
    expect(keptFolders).toContain('oss / ci')
    expect(keptFolders).toContain('Has its own settings')
    const del = screen.getByTestId('close-and-delete-folders').textContent ?? ''
    expect(del).toContain('oss / ci / worker')
    // Only worker is offered for deletion; oss and ci are kept chains.
    expect(screen.getAllByTestId('close-and-delete-folder').length).toBe(1)
    // 1 folder deletable (worker); oss and ci are kept chains.
    expect(screen.getByTestId('close-and-delete-confirm').textContent).toBe('Close 0 sessions and delete 1 folder')
  })

  it('stays open and says so when the server deleted fewer folders than asked', async () => {
    vi.spyOn(api, 'deleteChatSlot').mockResolvedValue({} as never)
    // Asked to delete 3, server kept one (a cron files into it) and deleted 2.
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: ['worker', 'ci'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const onClose = vi.fn()
    mount([slot({ key: 's1', title: 'alpha', folder_id: 'worker' })], null, new Set(), onClose)
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    expect(await screen.findByTestId('close-and-delete-notice')).toBeTruthy()
    expect(screen.getByTestId('close-and-delete-notice').textContent)
      .toBe('Some folders were kept because they still hold something the cleanup keeps.')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('rebuilds the frozen snapshot when the target folder changes under a new key', async () => {
    // Two independent top-level trees. The panel is keyed by folder id in
    // ChatSidebar, so switching the target folder remounts it and the frozen
    // snapshot is recomputed for the new tree.
    const TWO = [
      { id: 'A', name: 'A', parent_id: null },
      { id: 'B', name: 'B', parent_id: null },
    ] as unknown as ChatFolder[]
    const slots = [
      slot({ key: 'inA', title: 'in A', folder_id: 'A' }),
      slot({ key: 'inB', title: 'in B', folder_id: 'B' }),
    ]
    const close = vi.spyOn(api, 'deleteChatSlot').mockResolvedValue({} as never)
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { deleted: ['A'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    vi.spyOn(api, 'chatSlots').mockResolvedValue([] as never)
    const onClose = vi.fn()
    const { rerender } = mount(slots, null, new Set(), onClose, TWO)
    // First mount here has no key and targets `oss` (empty in TWO) — rerender
    // keyed for A, then keyed for B, as ChatSidebar does.
    rerender(panel(slots, TWO, null, onClose, TWO[0], 'A'))
    expect(screen.getByTestId('close-and-delete-sessions').textContent).toContain('in A')
    rerender(panel(slots, TWO, null, onClose, TWO[1], 'B'))
    expect(screen.getByTestId('close-and-delete-sessions').textContent).toContain('in B')
    expect(screen.getByTestId('close-and-delete-sessions').textContent).not.toContain('in A')
    fireEvent.click(screen.getByTestId('close-and-delete-confirm'))
    await waitFor(() => expect(close).toHaveBeenCalled())
    // Only B's session is closed; A's is untouched.
    expect(close.mock.calls.map(c => c[0])).toEqual(['inB'])
  })
})
