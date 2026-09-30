/** Folder state the lanes render from: the sort mode (server config), visibility
 *  (hide-when-empty, the reveal override, filter-menu hides), the subtree index and
 *  ancestor expansion, the filter-menu rows, and the folder writes. */
import { useMemo, useState, useRef, useCallback, useEffect, type Dispatch, type SetStateAction } from 'react'
import { useMutation, type QueryClient } from '@tanstack/react-query'
import { useOptimisticConfigPaths, setConfigPathValue } from '../settings/useOptimisticConfigPaths'
import { useFolderSortRead, type FolderSortConfigBody } from '../../hooks/useFolderSortMode'
import { type FolderSortMode, folderComparator, coveredByHiddenAncestor, collectFolderSubtreeIds } from '../../utils/folderTree'
import { api } from '../../api/client'
import { CHAT_FOLDERS_WRITE_KEY, invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import { computeActiveSubtree, folderIsHidden } from '../../utils/folderVisibility'
import type { ChatFolder } from '../../types'
import type { Slot } from './types'

/** The folder sort mode read from the shared config, its save, and the reorder hint. */
export function useFolderSort({ queryClient, mcCfg, mcCfgStatus, mcCfgError, mcCfgErrorUpdatedAt, setFolderActionError }: {
  queryClient: QueryClient
  mcCfg: FolderSortConfigBody | undefined
  mcCfgStatus: "error" | "pending" | "success"
  mcCfgError: Error | null
  mcCfgErrorUpdatedAt: number
  setFolderActionError: Dispatch<SetStateAction<string>>
}) {
  // ── Folder sort mode ──
  // How the folder tree orders siblings: the person's stored positions (custom),
  // a natural name order, or newest first. Server-side (dashboard.folder_sort, the
  // same kirocrewConfig query as the recency tint) rather than localStorage, because
  // the kirocrew-dashboard MCP server's chat_folder_tree lists folders in this
  // same order and an agent picking a before/after anchor from it must read the
  // sequence the person sees on every client. Every folder sort in the sidebar goes
  // through `folderCompare`; a site sorting with `bySidebarOrder` directly would
  // draw the stored order beside a tree the person has sorted by name.
  const cfgOverlay = useOptimisticConfigPaths(queryClient)
  // What this sidebar KNOWS about the mode, from the one derivation every
  // surface shares (`useFolderSortRead`): `mode` -- the person's once a config
  // body is on hand, the stored-order fallback otherwise; `known` -- that body
  // is on hand, fresh or cached; `error` -- a read that failed with nothing to
  // fall back on, held until a body arrives. Read from the SHARED cache, not
  // from the menu's own optimistic overlay: the folder-suggestion card, the
  // session menu, the Command Bar and the MCP tree all read that cache, and a
  // sidebar drawing a picked mode one round-trip before they do would show two
  // orders for one tree. So a pick re-sorts when the save lands -- the menu's
  // success write puts the accepted value into the cache at that instant and
  // every reader switches together -- and a refused save never leaves the
  // stored order. The fallback draws fine -- it is the order every earlier
  // build drew -- but a sibling drag may not write against it: that renumber is
  // computed from the DRAWN order, and only in the custom mode is that the
  // STORED order, so in any other mode it would rewrite the manual arrangement
  // behind a view the person is not looking at, and behind an unknown mode it
  // might. Hence the reorder affordance (each folder sortable's droppable side)
  // is on only when the mode is known to be custom and no failure is being
  // said. Known is the body, not the query status: a failed BACKGROUND refetch
  // keeps the previous body, react-query retries it on its own, and this
  // sidebar keeps drawing and acting on that body without a word -- while a
  // failure with no body is said once, on the sidebar's banner, and held through
  // the retry's pending phase rather than unmounted and remounted around it.
  const folderSortRead = useFolderSortRead({ data: mcCfg, status: mcCfgStatus, error: mcCfgError, errorUpdatedAt: mcCfgErrorUpdatedAt })
  const folderSortMode: FolderSortMode = folderSortRead.mode
  const folderCompare = useMemo(() => folderComparator(folderSortMode), [folderSortMode])
  const folderReorderable = folderSortRead.known && folderSortMode === 'custom' && folderSortRead.error === null
  // The first-load window: the folder list is drawn (its own query resolved)
  // while the mode is still being read -- not known, and no failure to say. The
  // rows' droppable side is off in that state, and a lift that then dies at the
  // drop would have nothing on screen to explain it (no banner: nothing failed;
  // no hint: the hint's words presume a known mode). The failed-read state's
  // rule is no refusal after the fact, so here the affordance is withheld
  // outright: both sides of every folder sortable off, no grab cursor, until
  // the read lands. Once it fails, the banner takes over and the lift returns
  // (re-parenting never needed the mode).
  const folderDragWithheld = !folderSortRead.known && folderSortRead.error === null
  // The withdrawn affordance said at the moment it is missed. A folder drag
  // outside Custom still LIFTS the row (the same gesture re-parents, and the
  // two cannot be told apart until the drop resolves), so a sibling drop that
  // opens no slot and moves nothing would otherwise read as broken -- weeks
  // after the mode was picked, with the only indicator a check mark inside a
  // closed menu. `handleSidebarDragEnd` sets this when such a drag ends with no
  // re-parent target. It clears on the person's NEXT interaction -- a pointer
  // or key landing anywhere but on the line itself -- on a switch back to
  // Custom, or when the next drag starts; never on a clock, which removed the
  // "Switch to Custom" action from under a hand reaching for it. A status, not
  // an error: nothing failed.
  const [folderReorderHint, setFolderReorderHint] = useState(false)
  const folderReorderHintRef = useRef<HTMLDivElement | null>(null)
  const showFolderReorderHint = useCallback(() => setFolderReorderHint(true), [])
  const hideFolderReorderHint = useCallback(() => setFolderReorderHint(false), [])
  useEffect(() => { if (folderReorderable) hideFolderReorderHint() }, [folderReorderable, hideFolderReorderHint])
  useEffect(() => {
    if (!folderReorderHint) return
    // Capture phase, document-wide: the interaction that retires the line may
    // be a click that another handler stops (a menu item, a drag handle), and
    // it may land outside this sidebar (the chat pane). Inside the line -- on
    // its own action -- it is not an interaction AWAY from it.
    const onInteract = (e: Event) => {
      const t = e.target
      if (t instanceof Node && folderReorderHintRef.current?.contains(t)) return
      setFolderReorderHint(false)
    }
    document.addEventListener('pointerdown', onInteract, true)
    document.addEventListener('keydown', onInteract, true)
    return () => {
      document.removeEventListener('pointerdown', onInteract, true)
      document.removeEventListener('keydown', onInteract, true)
    }
  }, [folderReorderHint])
  // Choosing a mode is a VIEW change: it writes one enum and never a folder's
  // stored `order`, which is what lets Custom restore the manual arrangement
  // exactly. The mutation rides the shared config overlay's SETTLE path --
  // token-guarded success write of the accepted value into the cache at this
  // one path (every reader of `['kirocrewConfig']` switches on that write),
  // then the settle-time refetch, and a failure report only while this save
  // still owns the path -- but not its optimistic display: see `folderSortRead`
  // above for why the tree waits for the save to land.
  const folderSortMut = useMutation({
    ...cfgOverlay.mutationOpts<FolderSortMode>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (mode: FolderSortMode) => api.patchConfig('dashboard.folder_sort', mode),
      path: () => 'dashboard.folder_sort',
      displayValue: v => v,
      applyToCache: (cached, mode) => setConfigPathValue(cached, 'dashboard.folder_sort', mode),
      // Nothing changed on screen (the tree never left the stored order), so the
      // person has to be told why their pick did not take, on the same notice the
      // other folder actions report through.
      onFailure: e => setFolderActionError(errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')),
    }),
    // ONE save on the wire at a time, in pick order. Two picks inside one
    // round-trip would otherwise be two concurrent PATCHes to the same path,
    // and the server persists whichever ARRIVES last -- a delayed first
    // request lands after the second and the stored mode is the earlier pick,
    // while the settle-time refetch then draws that earlier order as if it
    // were chosen. Dropping the first response client-side (a sequence number,
    // an abort) cannot fix that: the write already happened on the server. So
    // react-query's mutation scope queues a pick made while a save is in
    // flight -- its `onMutate` runs at once (the overlay's newest token is the
    // newest pick, so the superseded save's success never writes the cache),
    // its request starts only when the previous one has settled, success or
    // refusal -- and the last pick is both the last request the server sees
    // and the persisted one. A refusal behind a newer pick stays silent: the
    // newer save's own outcome is the one that matters and is the one
    // reported.
    scope: { id: 'dashboard.folder_sort' },
  })
  return {
    folderSortRead, folderSortMode, folderCompare, folderReorderable, folderDragWithheld, folderReorderHint,
    folderReorderHintRef, showFolderReorderHint, hideFolderReorderHint, folderSortMut,
  }
}

/** Which folders each slot sits in and which folders are hidden. */
export function useFolderVisibility({ folders, localSlots, filterHiddenFolders }: {
  folders: ChatFolder[]
  localSlots: Slot[]
  filterHiddenFolders: Set<string>
}) {
  const slotFolders = useMemo(() => {
    const valid = new Set(folders.map(f => f.id))
    const m: Record<string, string> = {}
    for (const s of localSlots) { if (s.folder_id && valid.has(s.folder_id)) m[s.key] = s.folder_id }
    return m
  }, [localSlots, folders])
  // Folder IDs that hold at least one ACTIVE slot, directly or via any
  // descendant folder. Computed from all `slots` (not filteredSlots) so a
  // search/filter never spuriously hides a folder that still holds work.
  const foldersWithActiveSubtree = useMemo(() => {
    const direct: string[] = []
    for (const s of localSlots) { const fid = slotFolders[s.key]; if (fid) direct.push(fid) }
    return computeActiveSubtree(folders, direct)
  }, [folders, localSlots, slotFolders])

  // A folder drops out of the active list only when the user hid it AND it is
  // currently empty (no active session in its subtree). Re-engaging a session
  // clears `hidden` server-side, so visibility is `!hidden || hasActive`.
  //
  // A reveal adds its target's ancestor chain to `revealForcedVisible`, which
  // overrides the hide for as long as this component lives. That is the whole
  // mechanism: the user asked to SEE this folder now, and "hide when empty" still
  // describes what they want on their next visit, so the rule is stepped over
  // rather than rewritten. It is component state on purpose — nothing is persisted,
  // so the override cannot outlive the visit it was needed for.
  const [revealForcedVisible, setRevealForcedVisible] = useState<Set<string>>(new Set())
  const isFolderHidden = useCallback(
    (f: ChatFolder) => !revealForcedVisible.has(f.id) && folderIsHidden(f, foldersWithActiveSubtree),
    [foldersWithActiveSubtree, revealForcedVisible],
  )

  // Folder IDs whose sessions are excluded from the flat lane because the
  // folder — or any ancestor — is unchecked in the filter menu's folder list.
  // Unchecking a parent hides its whole subtree, matching what the user sees
  // in the tree. Cycle-guarded: a hand-edited folders.json can contain a
  // parent_id loop and must not freeze the tab.
  const filterHiddenSubtree = useMemo(() => {
    if (filterHiddenFolders.size === 0) return new Set<string>()
    const byId = new Map(folders.map(f => [f.id, f]))
    const hidden = new Set<string>()
    for (const f of folders) {
      let cur: ChatFolder | undefined = f
      const visited = new Set<string>()
      while (cur && !visited.has(cur.id)) {
        visited.add(cur.id)
        if (filterHiddenFolders.has(cur.id)) { hidden.add(f.id); break }
        cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
      }
    }
    return hidden
  }, [folders, filterHiddenFolders])
  return { slotFolders, foldersWithActiveSubtree, setRevealForcedVisible, isFolderHidden, filterHiddenSubtree }
}

/** The rows that announce folders the filter menu is hiding, and their peeks. */
export function useFolderFilterReveal({ folderFilterActive, filterHiddenFolders, folders, isFolderHidden, folderCompare, boardLaneActive }: {
  folderFilterActive: boolean
  filterHiddenFolders: Set<string>
  folders: ChatFolder[]
  isFolderHidden: (f: ChatFolder) => boolean
  folderCompare: (a: ChatFolder, b: ChatFolder) => number
  boardLaneActive: boolean
}) {
  // List view (the folder tree) drops an unchecked folder's whole block —
  // header and sessions together. Only the folder's OWN id is checked here:
  // removing a parent block already takes its descendants with it.
  const isFolderFilteredOut = useCallback(
    (f: ChatFolder) => folderFilterActive && filterHiddenFolders.has(f.id),
    [folderFilterActive, filterHiddenFolders],
  )

  // Which reveal rows are peeked open. Deliberately EPHEMERAL (not persisted):
  // a reveal is a "let me look" gesture, not a preference — the folder is still
  // hidden, and the durable way back is the row's ⋯ → Show folder. Keyed by
  // container: 'root' for the top level, 'flat' for the flat lane, else the
  // parent folder's id.
  const [revealedContainers, setRevealedContainers] = useState<Set<string>>(new Set())
  const toggleReveal = useCallback((key: string) => {
    setRevealedContainers(prev => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }, [])
  // Collapse every peek the moment nothing is hidden any more, so a stale open
  // row can't linger after "Show all folders".
  useEffect(() => {
    if (!folderFilterActive) setRevealedContainers(prev => (prev.size === 0 ? prev : new Set()))
  }, [folderFilterActive])

  // Folders the filter is hiding, grouped by the container they would have
  // rendered in — 'root' for top-level, else the parent's id. A folder whose
  // ANCESTOR is hidden is deliberately absent: that whole block is already gone,
  // so its container is not on screen to host a row. That is what keeps the
  // announcement at exactly one level per hide.
  const hiddenByContainer = useMemo(() => {
    const m = new Map<string, ChatFolder[]>()
    if (!folderFilterActive) return m
    for (const f of folders) {
      if (isFolderHidden(f) || !filterHiddenFolders.has(f.id)) continue
      if (coveredByHiddenAncestor(f, folders, filterHiddenFolders)) continue
      const key = f.parent_id || 'root'
      const list = m.get(key)
      if (list) list.push(f); else m.set(key, [f])
    }
    for (const list of m.values()) list.sort(folderCompare)
    return m
  }, [folders, folderFilterActive, filterHiddenFolders, isFolderHidden, folderCompare])

  // Every folder the filter is hiding, flattened — the flat lane has no
  // containers to anchor to, so all hides collapse into its single row.
  const allHiddenFolders = useMemo(
    () => [...hiddenByContainer.values()].flat().sort(folderCompare),
    [hiddenByContainer, folderCompare],
  )

  /** Folders the person's uncheck is withholding from THE LANE ON SCREEN, announced.
   *
   *  ONE number, because it is reported in three places at once — the funnel, the
   *  filter menu's own Folders heading, and the board lane's notice — and two of
   *  those sit on screen together.
   *
   *  `filterHiddenFolders.size` is the raw checkbox set and is the wrong number for
   *  any of them: it counts a folder whose hidden ANCESTOR already took the whole
   *  block away, and keeps counting while a search suspends the hide entirely. Both
   *  announce rows as withheld that are either absent for another reason or not
   *  absent at all.
   *
   *  `allHiddenFolders` is the wrong number too, and in the opposite direction, for a
   *  BOARD: it drops a folder its own hide-when-empty attribute would remove, and a
   *  board column draws a folder block whatever that attribute says
   *  (`relevantFolders` filters on `isFolderFilteredOut` alone). So on a board the
   *  uncheck does take that block away, and dropping it announces nothing while the
   *  header disappears — the exact traceless hide this row exists to end. The other
   *  lanes narrow by `isFolderHidden` themselves, so there the uncheck takes nothing
   *  a reader would otherwise have seen, and counting it would over-report.
   *
   *  Hence one predicate and two scopes, not two unrelated counts. A count and not a
   *  list, because nothing renders this population: the reveal rows draw from
   *  `hiddenByContainer`, which is grouped by container and ordered for display.
   */
  const hiddenFolderCount = useMemo(() => {
    if (!folderFilterActive) return 0
    let n = 0
    for (const f of folders) {
      if (!filterHiddenFolders.has(f.id)) continue
      if (!boardLaneActive && isFolderHidden(f)) continue
      if (coveredByHiddenAncestor(f, folders, filterHiddenFolders)) continue
      n += 1
    }
    return n
  }, [folders, folderFilterActive, filterHiddenFolders, isFolderHidden, boardLaneActive])
  return {
    isFolderFilteredOut, revealedContainers, toggleReveal, hiddenByContainer, allHiddenFolders,
    hiddenFolderCount,
  }
}

/** The filter menu folder rows, in tree order with direct counts. */
export function useFolderFilterRows({ folderTree, folders, folderCompare, filterHiddenFolders, filterHiddenSubtree }: {
  /** The row model's folder index: the filtered rows filed directly in each folder. */
  folderTree: { rowsIn: (folderId: string) => Slot[] }
  folders: ChatFolder[]
  folderCompare: (a: ChatFolder, b: ChatFolder) => number
  filterHiddenFolders: Set<string>
  filterHiddenSubtree: Set<string>
}) {
  // Folder rows for the filter menu: every folder in tree order, each with the
  // count of flat-lane sessions filed directly in it, and whether an unchecked
  // ancestor is already hiding it (that row renders inert).
  const folderFilterRows = useMemo(() => {
    // Same roots + childrenOf walk the "New chat in folder" menu uses, with a
    // visited set so a parent_id cycle terminates instead of recursing forever.
    const roots = folders.filter(f => !f.parent_id).sort(folderCompare)
    const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid).sort(folderCompare)
    const rows: { folder: ChatFolder; depth: number; count: number; hidden: boolean; hiddenByAncestor: boolean }[] = []
    const visited = new Set<string>()
    const walk = (list: ChatFolder[], depth: number) => {
      for (const f of list) {
        if (visited.has(f.id)) continue
        visited.add(f.id)
        rows.push({
          folder: f,
          depth,
          count: folderTree.rowsIn(f.id).length,
          hidden: filterHiddenFolders.has(f.id),
          hiddenByAncestor: !filterHiddenFolders.has(f.id) && filterHiddenSubtree.has(f.id),
        })
        walk(childrenOf(f.id), depth + 1)
      }
    }
    walk(roots, 0)
    // Orphans (parent_id pointing at a deleted folder, or inside a cycle) are
    // unreachable from the roots — append them so no folder is unlistable.
    for (const f of folders) {
      if (visited.has(f.id)) continue
      visited.add(f.id)
      rows.push({
        folder: f,
        depth: 0,
        count: folderTree.rowsIn(f.id).length,
        hidden: filterHiddenFolders.has(f.id),
        hiddenByAncestor: !filterHiddenFolders.has(f.id) && filterHiddenSubtree.has(f.id),
      })
    }
    return rows
  }, [folders, folderTree, filterHiddenFolders, filterHiddenSubtree, folderCompare])
  return { folderFilterRows }
}

/** Folder create, delete and optimistic update. */
export function useFolderMutations({ queryClient, setFolderActionError, folders }: {
  queryClient: QueryClient
  setFolderActionError: Dispatch<SetStateAction<string>>
  folders: ChatFolder[]
}) {
  // Folder mutations
  const createFolderMutation = useMutation({
    mutationFn: (v: { name: string; parentId?: string; projectDir?: string; defaultAgent?: string; color?: string; icon?: string; tags?: string[]; steeringDirs?: string[] }) =>
      api.createChatFolder(v.name.trim(), v.parentId, {
        project_dir: v.projectDir || undefined,
        default_agent: v.defaultAgent || undefined,
        color: v.color || undefined,
        icon: v.icon || undefined,
        tags: v.tags && v.tags.length > 0 ? v.tags : undefined,
        steering_dirs: v.steeringDirs && v.steeringDirs.length > 0 ? v.steeringDirs : undefined,
      }),
    // Through the idle gate, not a direct invalidate: a refetch here would
    // land inside a still-pending update's optimistic window.
    onSuccess: () => invalidateFoldersWhenIdle(queryClient),
    onError: (e) => setFolderActionError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))),
  })
  const deleteFolderMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatFolder(id),
    // Through the idle gate, not a direct invalidate: a refetch here would
    // land inside a still-pending update's optimistic window.
    onSuccess: () => invalidateFoldersWhenIdle(queryClient),
    onError: (e) => setFolderActionError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))),
  })
  const updateFolderMutation = useMutation({
    mutationKey: CHAT_FOLDERS_WRITE_KEY,
    mutationFn: ({ id, body }: { id: string; body: object; onCommitted?: () => void }) => api.updateChatFolder(id, body),
    onMutate: async ({ id, body }) => {
      await queryClient.cancelQueries({ queryKey: ['chat-folders'] })
      const before = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])?.find(f => f.id === id)
      queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old => (old ?? []).map(f => f.id === id ? { ...f, ...body } : f))
      return { id, body, before }
    },
    // The ack callback rides the mutation VARIABLES, not a per-call
    // `mutate(..., { onSuccess })`: TanStack Query's observer only invokes the
    // LATEST call's per-call callbacks, so a second mutation through this same
    // hook (a rename, a collapse toggle) before the drag's PATCH settled would
    // silently drop the drag's ack — and its undo offer would never go live.
    onSuccess: (_data, vars) => vars.onCommitted?.(),
    // Field-scoped compare-and-set rollback, NOT a whole-list snapshot restore:
    // a snapshot taken before this mutation would clobber every LATER
    // concurrent optimistic change (another move, a rename, a collapse toggle)
    // when this one fails. Restore only the fields this mutation set, and only
    // where the cache still holds this mutation's own optimistic value. Same
    // rationale as ArtifactsPage's updateFolderMut — the drag-undo offer this
    // PR arms observes the cache, so a rollback that momentarily rewrites an
    // UNRELATED folder move would retire that move's valid offer.
    onError: (err, _vars, ctx) => {
      // The rollback below restores the cache; this names the failure so the
      // rename / collapse / move that just snapped back is not read as a dead click.
      setFolderActionError((errMessage(err) || i18nT('components.errorBoundary.something_went_wrong')))
      if (!ctx?.before) return
      const { id, body, before } = ctx
      queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old => (old ?? []).map(f => {
        if (f.id !== id) return f
        const cur = { ...f } as Record<string, unknown>
        const opt = body as Record<string, unknown>
        const prev = before as unknown as Record<string, unknown>
        for (const k of Object.keys(opt)) if (cur[k] === opt[k]) cur[k] = prev[k]
        return cur as unknown as ChatFolder
      }))
    },
    // Refetch once the LAST overlapping write settles (see
    // invalidateFoldersWhenIdle). An earlier write's settle would otherwise
    // refetch while a later one is still unacknowledged, and that reply would
    // paint the later write's pre-PATCH value over its optimistic one.
    onSettled: () => invalidateFoldersWhenIdle(queryClient),
  })
  const toggleCollapse = useCallback((id: string) => {
    const f = folders.find(x => x.id === id)
    if (f) updateFolderMutation.mutate({ id, body: { collapsed: !f.collapsed } })
  }, [folders, updateFolderMutation])
  return { createFolderMutation, deleteFolderMutation, updateFolderMutation, toggleCollapse }
}

/** The folder writes, typed once for the owners that call them. */
export type FolderMutations = ReturnType<typeof useFolderMutations>

/** The subtree index and the ancestor expansion a reveal or create needs. */
export function useFolderTree({ folders, updateFolderMutation, clearBoardCollapse }: {
  folders: ChatFolder[]
  updateFolderMutation: FolderMutations['updateFolderMutation']
  clearBoardCollapse: (folderId: string, columnId?: string) => void
}) {
  // Subtree sets for every folder, recomputed only when the folder list
  // changes — the facade's render paths (menu target filters + drag data)
  // do map lookups instead of re-walking the tree on every render pass.
  const folderSubtrees = useMemo(() => {
    const m = new Map<string, Set<string>>()
    for (const f of folders) m.set(f.id, collectFolderSubtreeIds(folders, f.id))
    return m
  }, [folders])

  /**
   * Open `folderId` and every collapsed folder above it, so a reveal aimed at or
   * inside it has something to scroll to.
   *
   * Both reveals start from a folder that must itself open: a session reveal starts
   * at the row's CONTAINER, and a folder reveal starts at its TARGET. There is no
   * caller that wants the ancestors without the folder they were reached through,
   * which is why this takes no "include self" switch — an earlier version did, and
   * it silently stopped expanding the folder a revealed session was filed in.
   *
   * Cycle-guarded: `folders.json` is hand-editable and a `parent_id` loop must not
   * hang the tab. Board columns keep per-column collapsed overrides, which are
   * dropped for each folder on the path as well — otherwise the revealed row stays
   * hidden in whichever column's local state still holds an ancestor shut.
   */
  const expandFolderAncestors = useCallback((folderId: string) => {
    const visited = new Set<string>()
    const expand = (fid: string) => {
      if (visited.has(fid)) return
      visited.add(fid)
      const f = folders.find(x => x.id === fid)
      if (f?.collapsed) updateFolderMutation.mutate({ id: fid, body: { collapsed: false } })
      clearBoardCollapse(fid)
      if (f?.parent_id) expand(f.parent_id)
    }
    expand(folderId)
  }, [folders, updateFolderMutation, clearBoardCollapse])
  return { folderSubtrees, expandFolderAncestors }
}

/** The tree lane's root folders. Its ungrouped rows are the row model's (`./rows`). */
export function useRootFolderLanes({ folders, folderCompare, isFolderHidden, isFolderFilteredOut }: {
  folders: ChatFolder[]
  folderCompare: (a: ChatFolder, b: ChatFolder) => number
  isFolderHidden: (f: ChatFolder) => boolean
  isFolderFilteredOut: (f: ChatFolder) => boolean
}) {
  const rootFolders = useMemo(() => folders.filter(f => !f.parent_id).sort(folderCompare), [folders, folderCompare])
  const visibleRootFolders = useMemo(() => rootFolders.filter(f => !isFolderHidden(f) && !isFolderFilteredOut(f)), [rootFolders, isFolderHidden, isFolderFilteredOut])
  const rootFolderIds = useMemo(() => visibleRootFolders.map(f => f.id), [visibleRootFolders])
  return { rootFolders, visibleRootFolders, rootFolderIds }
}
