/** Server-owned caches the socket keeps fresh by push.
 *
 *  React Query entries (and the member-projection and thread-reply live
 *  stores) hold server reads with no polling; these frames tell a tab which
 *  of them moved. They are one-shot broadcasts, so a reconnect re-reads the
 *  caches a missed frame would leave stale (`refreshServerStateAfterReconnect`). */
import type { QueryClient } from '@tanstack/react-query'
import type { AppDispatch } from '../../store'
import { patchSlotSourceLinks } from '../../store/dashboardSlice'
import { purgeDocumentBodiesForRedactionChange } from '../usePanelTabs'
import { isArtifactEditing } from '../../utils/artifactEditGuard'
import { api } from '../../api/client'
// From the leaf module, not `api/client`: the many tests that mock the client
// do not export ApiError, and an `instanceof` against a missing mock export
// throws inside the reconnect heal's own catch.
import { ApiError } from '../../api/apiError'
import { forgetUnobservedMemberThreads, MEMBERS_ROSTER_QUERY_KEY, MEMBER_PROJECTIONS_QUERY_PREFIX } from '../../api/membersQuery'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { threadLiveStore, type ThreadAnchorFrame } from '../../state/threadLiveStore'
import { threadQueryKey, threadsQueryKey } from '../../api/threads'
import { applyStatusDelta, parseStatusDelta } from '../../utils/pullRequestStatusDelta'
import { slotChangeUrls } from '../../utils/pullRequestLinks'
import type { ChatSlot, PullRequestStatusBatch } from '../../types'
import { emitArtifactDeleted } from './browserEvents'
import type { FrameData } from './frames'

/** After a reconnect, re-read the owner's credential-redaction switch and purge
 *  this document's file bodies when that could matter (the
 *  `credential_redaction_changed` push has no replay):
 *   - the document had read the switch and its position CHANGED while the
 *     socket was down; or
 *   - the document had NOT read the switch (never visited Settings) and it is
 *     now ON -- a file opened raw while OFF may be on screen, and nothing else
 *     would ever re-read it.
 *  Unmoved, or unknown-and-still-OFF, costs nothing: a transient drop must not
 *  close every diff tab and empty every clean file body. A non-owner's 403 is
 *  swallowed: the card handles that; the socket has nothing to purge for. */
let redactionSwitchUnreadable = false
/** Test seam: forget a latched refusal. */
export function __resetRedactionHealForTests(): void { redactionSwitchUnreadable = false }

export async function healRedactionSwitchAfterReconnect(qc: QueryClient): Promise<void> {
  // A document the owner gate refused once (a Slack allow-listed non-owner's
  // `!dashboard`) is refused on every reconnect too, and each ask writes an
  // audited refusal for a subject that took no action: ask once, then stop.
  if (redactionSwitchUnreadable) return
  const before = qc.getQueryData<{ enabled: boolean }>(['credential-redaction'])
  let after: { enabled: boolean; changed_at?: string } | undefined
  try {
    after = await qc.fetchQuery({ queryKey: ['credential-redaction'], queryFn: () => api.credentialRedaction(), staleTime: 0 })
  } catch (e) {
    if (e instanceof ApiError && (e.status === 403 || e.status === 401)) redactionSwitchUnreadable = true
    return
  }
  if (!after) return
  const moved = before !== undefined && after.enabled !== before.enabled
  // Unknown position and ON: purge only if the switch has EVER been flipped
  // (`changed_at` set). In the shipped default -- ON, never flipped, Settings
  // never opened -- no raw body can exist, and a transient drop must not close
  // every diff tab and empty every clean file body for nothing.
  const unknownAndNowOn = before === undefined && after.enabled && !!after.changed_at
  if (moved || unknownAndNowOn) purgeDocumentBodiesForRedactionChange(qc)
}

/**
 * Invalidate React Query caches for keys that previously relied on the
 * `refreshTrigger` counter being part of their queryKey.  Calling
 * `invalidateQueries` refetches **in-place** (keeping the cached data visible
 * to the UI) instead of minting a brand-new cache entry with `undefined` data
 * — which is what caused the flash-to-empty bug (#4132, #4179).
 */
export function invalidateRefreshQueries(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ['cron-jobs'] })
  // Prefix match on purpose: `['crons', 'crew-wake', <crew>]` caches the one global cron
  // payload once per crewmate whose schedules pane has been opened (the crew editor's and
  // the Crewmates panel's). Invalidating only `['cron-jobs']` refreshed the Schedule page
  // and left every one of those entries stale, so a schedule changed anywhere else kept
  // its old status under each crewmate until that entry happened to refetch.
  qc.invalidateQueries({ queryKey: ['crons'] })
  qc.invalidateQueries({ queryKey: ['cron-history-all'] })
  qc.invalidateQueries({ queryKey: ['spawn-list'] })
  qc.invalidateQueries({ queryKey: ['sessions-context'] })
  qc.invalidateQueries({ queryKey: ['sessions-usage'] })
  qc.invalidateQueries({ queryKey: ['agents-installed'] })
  qc.invalidateQueries({ queryKey: ['mcp-tools'] })
  // Prefix match on purpose: the Crew Members roster lives under this key
  // (api/membersQuery.ts) and refreshes with the registry.
  qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
  qc.invalidateQueries({ queryKey: ['default-agent'] })
  qc.invalidateQueries({ queryKey: ['workspaces'] })
  qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
  // These answers derive from the config AND, for an `auto` default, the
  // installed agent spec rebuilt by the server's config applier. A refresh
  // frame is emitted only after that applier completes, so invalidate the
  // infinite-stale caches here rather than relying solely on a changed config
  // value to mint a new key. This also covers external/CLI config writes.
  qc.invalidateQueries({ queryKey: ['resolved-model'] })
  qc.invalidateQueries({ queryKey: ['agent-resolved-model'] })
  // Prefix match on purpose: covers the filtered library list
  // (['artifacts', {tag, kind}]) and the tag-options read
  // (['artifacts', 'all-tags']) in one shot. The `artifact_update` frame
  // already heals these on live mutations; this covers the server's generic
  // refresh broadcast so an errored or stale list recovers without a reload
  // (#10867).
  qc.invalidateQueries({ queryKey: ['artifacts'] })
  // The folder list fails alongside it under the same trigger (an
  // auth-expired window 403s every endpoint) and renders `?? []` the same
  // way — heal both or the library recovers half-empty (#10867).
  qc.invalidateQueries({ queryKey: ['artifact-folders'] })
}

/** Reconnect: re-read the caches whose one-shot frames may have been missed
 *  while the socket was down. */
export function refreshServerStateAfterReconnect(queryClient: QueryClient): void {
  // A summary regenerated while the socket was down pushed a
  // `session_summary` event nobody received, and the panel does not poll,
  // so without this the stale summary persists until the tab remounts.
  // Invalidate every slot's summary (the key is per-slot and we cannot
  // know which ones moved); react-query only refetches the observed ones.
  queryClient.invalidateQueries({ queryKey: ['session-summary'] })
  // A missed removal may have reused a slot key. Discard cached content
  // and cancel old reads before refetching observed cards.
  queryClient.resetQueries({ queryKey: ['dashboard-card'] })
  queryClient.invalidateQueries({ queryKey: ['command-center'] })
  // Same one-shot problem for the artifact library: `artifact_update`
  // frames pushed while the socket was down were never delivered, and a
  // list query that ERRORED during the gap (gateway restart 403s /
  // connection refused) holds no data at all. Invalidate the whole
  // ['artifacts'] prefix (filtered list + all-tags) so a recovered
  // window heals without a manual hard refresh (#10867). The folder
  // list errors under the same trigger — heal it too, or the library
  // comes back with its folders missing.
  queryClient.invalidateQueries({ queryKey: ['artifacts'] })
  queryClient.invalidateQueries({ queryKey: ['artifact-folders'] })
  // `credential_redaction_changed` is pushed to CONNECTED owner sockets
  // with no replay, so a flip made from another window while this socket
  // was down never reached this document. Re-read the switch and, ONLY if
  // its position differs from the one this document last held, drop every
  // file body (react-query and open tabs) exactly as the frame would have.
  // Not unconditionally: a transient drop (sleep/wake, Wi-Fi change,
  // gateway restart) must not close every diff tab and empty every clean
  // file body when the switch never moved. A document that never read the
  // switch has nothing to compare and nothing raw to drop.
  void healRedactionSwitchAfterReconnect(queryClient).catch(() => { /* a heal that cannot run leaves the document as it was */ })
  // Same one-shot problem for a thread's anchor: a `chat.thread_anchor` frame
  // announcing a thread opened or closed while the socket was down was never
  // delivered, so the live store would name a stale slot or miss a close
  // forever. Drop every row (the anchor index is the truth) and refetch every
  // observed anchor and footer count.
  threadLiveStore.reset()
  queryClient.invalidateQueries({ queryKey: ['chat-thread'] })
  queryClient.invalidateQueries({ queryKey: ['chat-threads'] })
  // A dropped socket is the one client-visible sign the gateway may have
  // restarted — and a restart drops an unmessaged member slot while its
  // binding survives. The Crew Members page mounts a cached thread key
  // straight away on a return visit (api/membersQuery.ts), so a key
  // confirmed BEFORE the drop is no longer known-mountable: forget the
  // ones nobody is looking at, so the next open waits for the thread
  // endpoint's answer again; the mounted one is re-confirmed by the page
  // itself on this same reconnect (and must NOT be cleared here — it
  // holds the pane, and the draft typed into it).
  forgetUnobservedMemberThreads(queryClient)
}

/** The owner flipped the credential-redaction switch, possibly in ANOTHER
 *  browser tab: this document must drop every file body it holds too (a file
 *  read while the switch was off is raw in the react-query caches and in open
 *  side-panel tabs) and re-read the switch, so no dashboard document keeps
 *  showing raw credentials after redaction is back on. Owner sockets only
 *  receive this frame. */
export function handleCredentialRedactionChanged(queryClient: QueryClient, d: { enabled?: unknown; changed_at?: unknown } | undefined): void {
  // Seed the switch entry from the frame's own payload FIRST, so a
  // document that never mounted the Settings card still knows the
  // position it now runs under (the reconnect heal compares against
  // it); the invalidate then re-reads the authoritative record.
  if (d && typeof d.enabled === 'boolean') {
    queryClient.setQueryData(['credential-redaction'], { enabled: d.enabled, changed_at: typeof d.changed_at === 'string' ? d.changed_at : '' })
  }
  queryClient.invalidateQueries({ queryKey: ['credential-redaction'] })
  purgeDocumentBodiesForRedactionChange(queryClient)
}

/** Live artifact refresh: the backend broadcasts from the artifact mutation
 *  funnel (create / content PATCH / revert / relocate / pull / delete).
 *  Invalidate the per-slug queries so any open view — detail page, popout,
 *  the companion panel's left pane — re-renders the new version immediately.
 *  Every window has its own WS, so no BroadcastChannel is needed. The library
 *  list is invalidated too (create/delete change it; content updates bump its
 *  updated_at ordering). */
export function handleArtifactUpdate(queryClient: QueryClient, data: FrameData): void {
  const slug = (data as { slug?: string }).slug
  if (slug) {
    if ((data as { deleted?: boolean }).deleted) {
      // Notify, but deliberately do NOT evict ['artifact', slug].
      // Evicting drops the detail page's query data, which re-renders it
      // into a loading/404 state and unmounts the editor — taking an
      // unsaved edit buffer with it. That would defeat the deletion
      // listener's dirty-page guard, which exists precisely so the user
      // can still copy their work out. A clean page navigates away, and a
      // dirty one keeps its cached content; neither needs the eviction,
      // and a genuine refetch 404s on its own because the artifact is
      // gone server-side.
      emitArtifactDeleted(slug)
    } else if (isArtifactEditing(slug)) {
      // A human has an unsaved buffer open on this artifact. Refetching
      // would move the editor's baseline while the buffer keeps the older
      // text, so the next Save would overwrite whatever just arrived.
      // Leave the content cache alone; the page reloads it on save or
      // cancel. Comments/events carry no edit buffer, so they still
      // refresh — only content is withheld.
      queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
    } else {
      queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
    }
    queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    // The idle dock does not poll, so a newly published task
    // dashboard reaches it through this frame.
    queryClient.invalidateQueries({ queryKey: ['command-center', 'artifacts'] })
  }
}

/** One member's projected value moved. The server wraps every broadcast as
 *  { type, data }, so the fields ride under `data`. Apply only a well-formed
 *  frame: the store's higher-seq-wins drops a stale or replayed seq, but a
 *  missing slug/key/seq is a malformed frame that must not touch the store at
 *  all. */
export function handleMemberProjection(data: FrameData): void {
  const pf = (data ?? {}) as { slug?: unknown; key?: unknown; seq?: unknown; value?: unknown }
  if (typeof pf.slug === 'string' && pf.slug && typeof pf.key === 'string' && pf.key && typeof pf.seq === 'number') {
    memberProjectionStore.apply(pf.slug, pf.key, pf.value, pf.seq)
  }
}

/** Sent once per connection before any member_projection frame: the server's
 *  authoritative lastSeq per slug. Truncate held rows that ran ahead of it (a
 *  torn tail rolled back after a restart). */
export function handleMembersSubscribed(queryClient: QueryClient, data: FrameData): void {
  const seqs = ((data ?? {}) as { lastSeqs?: unknown }).lastSeqs
  if (seqs && typeof seqs === 'object') {
    const dropped = memberProjectionStore.truncateAll(
      seqs as { [slug: string]: number },
    )
    if (dropped) {
      // A drop is correct but incomplete: the row above the server's seq
      // recorded something that did not happen, and removing it leaves the
      // card with no value where the truth is whatever the server holds at
      // its own seq. The store is a cache and cannot produce that, so the
      // reads that own those values are refetched -- seeding is
      // higher-seq-wins, so this restores the authoritative value without
      // overwriting anything newer that arrives meanwhile.
      //
      // BOTH reads, because the truncation drops every key a slug holds
      // while each read owns only some of them: a roster row carries the
      // `roster` view, and the open member's activity, wake and driving
      // views come from its own per-member projections read. Invalidating
      // the roster alone would leave the drawer blank until something else
      // happened to refetch it.
      //
      // RESET, not invalidate, for BOTH reads. Invalidating a query with
      // no enabled observer only marks it stale: its pre-rollback block
      // stays in cache, and the next mount runs `select` over that block
      // before any refetch lands, seeding the store at the sequence the
      // server just rolled back. Higher-seq-wins then REJECTS the
      // authoritative lower-seq baseline the refetch returns, so the
      // rolled-back values repaint as live with no self-correcting path.
      // Resetting drops the cached block, so there is nothing stale to
      // seed from, and an active query still refetches.
      //
      // Neither read is exempt. The per-member one is disabled while no
      // member is open. The roster's own observer outside the members page
      // is the crewmates gate, which holds it `enabled: eligible`, so the
      // roster query has no enabled observer either once that page
      // unmounts. The cost is a fetch where a fresh cache would have
      // served, and only on a torn tail -- a gateway restart -- against a
      // wrong value that would otherwise win permanently.
      queryClient.resetQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      queryClient.resetQueries({ queryKey: MEMBER_PROJECTIONS_QUERY_PREFIX })
    }
  }
}

/** A thread was opened or closed on one of this slot's messages. The thread's
 *  own MESSAGES arrive on the thread slot's ordinary frames — a thread is a real
 *  session — so this frame announces the ANCHOR only: the live store gets the
 *  slot it resolved to, and the anchor index plus the footer counts are refetched. */
export function handleThreadAnchor(queryClient: QueryClient, data: FrameData): void {
  const frame = data as ThreadAnchorFrame
  if (typeof frame.slot !== 'string' || typeof frame.mid !== 'string') return
  threadLiveStore.apply(frame)
  // Unconditional, unlike the reply frames this replaced: an anchor frame is
  // only ever sent for an open or a close, and both change the index.
  queryClient.invalidateQueries({ queryKey: threadQueryKey(frame.slot, frame.mid) })
  queryClient.invalidateQueries({ queryKey: threadsQueryKey(frame.slot) })
}

/** A pull request's lifecycle/CI status changed on the gateway. Patch the
 *  strip's cached batch straight away (no poll wait) and refetch the detail
 *  payload so both surfaces — on every owner window — track the same state
 *  instead of disagreeing until the next poll. */
export function handleSourceStatus(dispatch: AppDispatch, queryClient: QueryClient, data: FrameData): void {
  const delta = parseStatusDelta(data)
  if (!delta) return
  // Cancel any in-flight batched-status fetch first. Without this, a
  // status poll that started before this delta can resolve AFTER the
  // setQueriesData below and overwrite the authoritative pushed value
  // with its stale snapshot for a full TTL. cancelQueries aborts the
  // pending fetch so it cannot clobber the patch; the retained poll
  // (and the detail invalidation below) reconcile from here.
  queryClient.cancelQueries({ queryKey: ['pull-request-statuses'] })
  queryClient.setQueriesData<PullRequestStatusBatch>(
    { queryKey: ['pull-request-statuses'] },
    batch => applyStatusDelta(batch, delta),
  )
  // Patch the sidebar chips too: those render from the Redux `slots`
  // payload (`source_links[].state/ci`), NOT react-query, so a delta
  // that only touched the query caches would leave the sidebar glyph
  // stale until an unrelated slots broadcast — the same chip↔panel
  // divergence this feature removes, recreated on the sidebar.
  dispatch(patchSlotSourceLinks({ url: delta.url, state: delta.state, ci: delta.ci }))
  // Invalidate the detail payload for EVERY changed delta, regardless
  // of origin. A 'detail'-origin delta is produced by one window's full
  // fetch; only that window received the fresh HTTP payload, so other
  // owner windows must refetch too or their staleTime:Infinity detail
  // query keeps rendering the pre-change lifecycle — the exact chip↔
  // panel divergence this feature fixes, just across windows. The
  // initiating window's refetch is harmless: it hits the gateway's
  // still-warm full-payload cache (same value, no re-projection, no new
  // delta), so there is no feedback loop.
  queryClient.invalidateQueries({ queryKey: ['pull-request-source', delta.url] })
  queryClient.invalidateQueries({ queryKey: ['pull-request-checks', delta.url] })
}

/** Turn boundary: the finished turn is the likeliest moment for this
 *  session's PRs to have moved (comments, mergeability, a pushed revision) —
 *  changes the lightweight status delta does NOT carry. Invalidate the detail
 *  queries of THIS slot's own pull requests so they refetch. For the ACTIVE
 *  slot, refetch now (the panel is on screen). For a BACKGROUND slot, only
 *  MARK stale (refetchType: 'none'): its detail query is staleTime:Infinity,
 *  so without this it would stay "fresh" forever and render pre-turn data
 *  when the user later switches to it — but refetching an off-screen PR every
 *  background turn would be wasteful, so defer the fetch to its next mount.
 *
 *  Scoped to the slot's own links, never the whole key family: an unscoped
 *  invalidation marked EVERY session's PR stale on EVERY turn anywhere, so a
 *  panel reopened while any chat was running always refetched (five provider
 *  subprocesses per open) even though nothing about that PR had changed.
 *
 *  The ACTIVE slot additionally refetches whatever detail query is MOUNTED —
 *  the PR on screen: the slots payload names only the first few chips, so a
 *  session with more PRs than chips could otherwise have the very PR the user
 *  is looking at fall outside the scoped set. `refetchQueries` with
 *  `type: 'active'` is the primitive for that: it refetches the mounted
 *  queries only and marks nothing else stale. (`invalidateQueries` with
 *  `refetchType: 'active'` would NOT do — refetchType limits only the
 *  refetch, the stale marking still hits every cached PR.) A background
 *  slot's overflow PRs are left to the status-delta path (lifecycle / CI /
 *  merge pair); their comments may lag until the next event or remount. */
export function refreshPullRequestsAfterTurn(queryClient: QueryClient, slots: ChatSlot[], slot: string, isActive: boolean): void {
  const refetchType = isActive ? 'active' : 'none'
  if (isActive) {
    void queryClient.refetchQueries({ queryKey: ['pull-request-source'], type: 'active' })
  }
  for (const url of slotChangeUrls(slots, slot)) {
    queryClient.invalidateQueries({ queryKey: ['pull-request-source', url], refetchType: 'none' })
  }
  queryClient.invalidateQueries({ queryKey: ['pull-request-statuses'], refetchType })
}
