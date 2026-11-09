/** One sidebar-level menu item: remove every source-link chip whose pull
 *  request / change request has MERGED, across every session in the list, in a
 *  single step. An OPT-IN ADDITION — nothing is removed until the user invokes
 *  it, and a chip renders exactly as it does today until then.
 *
 *  Why this re-reads status rather than trusting the chip's own `state`:
 *  the chip's `state` field is whatever the slots payload happened to carry.
 *  Removal is durable (an unlink tombstones the link in the session's dismissed
 *  set), so deciding off a value that could be stale risks removing a chip for a
 *  change that has NOT merged — the one correctness bug this action must not
 *  have. So the action asks the provider status endpoint (`pullRequestStatuses`)
 *  for each change's status — the short-TTL provider cache the chips already
 *  use, which also schedules a background refresh — and removes ONLY the ones it
 *  reports `merged`. `merged` is a TERMINAL provider state — a change cannot
 *  leave it once the provider has reported it — so acting on a `merged` answer
 *  is safe even from the cache; anything the endpoint does not report `merged`
 *  is left in place, which is the guard against removing an unmerged change. */
import { useId, useState } from 'react'
import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import { GitMerge, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { useConnected } from '../../hooks/useConnected'
import { useTranslation } from 'react-i18next'
import { useAppStore } from '../../store'
import type { ChatSlot, PullRequestStatus } from '../../types'
import { DropdownMenuItem, DropdownMenuSeparator } from '../../components/ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import { findReport } from '../../utils/errorReport'

/** The server reads at most this many urls per status request
 *  (`source_providers.py` STATUS_URLS_MAX); urls past it get no answer. Send in
 *  chunks of this size and merge the replies, or a sidebar with more than this
 *  many distinct change urls would leave every merged change past the cutoff
 *  untouched on every invocation. */
const STATUS_URLS_PER_REQUEST = 64

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size))
  return out
}

/** Mutation key for the sweep. The outcome (error / pending / progress) lives
 *  in React Query's mutation cache, NOT in component state, so it survives the
 *  menu content unmounting mid-sweep: reopening the menu reads the retained
 *  outcome back through `useMutationState`, the same way the per-session unlink
 *  in `SourceLinksSubmenu` keeps its error across a dismissal. */
const SWEEP_KEY = ['remove-merged-cr-chips']

interface SweepProgress { done: number; total: number }

/** The generation triple the unlink DELETE validates as `expect`
 *  (`<row_identity>|<created_at>|<linked_session_key>`). Identical to the
 *  frontend `slotGeneration` in `SourceLinksSubmenu`; a removal that would land
 *  on a recreated / rebound session at the same key is refused (409) by the
 *  backend because this string no longer matches. */
function slotGeneration(slot: ChatSlot): string {
  return `${slot.row_identity ?? slot.key}|${slot.created ?? ''}|${slot.linked_session_key ?? ''}`
}

/** One change-kind chip we might remove: enough to fetch its provider status,
 *  key its removal, and target the right session. */
interface Candidate {
  slotKey: string
  identity: string
  url: string
  expect: string
}

/** Every change-kind, unlinkable source link across all sessions. Issue chips
 *  are excluded (they carry no merge state), as are chips with no `identity`
 *  (an older gateway did not send one, so they cannot be unlinked).
 *
 *  Uses the budgeted `source_links` the slots payload already carries. A
 *  session with more links than the per-card budget (`source_links_total >
 *  source_links.length`) has the rest only behind its "+N" expand; this sweep
 *  does NOT fetch those, so it covers the chips that are actually on screen —
 *  the ones the user sees cluttering the list — and a second invocation sweeps
 *  any that a later slots push brings into the budget. Fetching every session's
 *  full list would be a burst of N GETs on a large sidebar; keeping to the
 *  budgeted set is what holds this to a single, cheap opt-in action. */
function collectCandidates(slots: ChatSlot[]): Candidate[] {
  const out: Candidate[] = []
  const seen = new Map<string, Set<string>>()
  for (const slot of slots) {
    const links = slot.source_links
    if (!links) continue
    const expect = slotGeneration(slot)
    for (const link of links) {
      if ((link.kind ?? 'change') === 'issue') continue
      if (!link.identity) continue
      // De-dupe per (slot, identity): a link cannot be unlinked twice, and the
      // same change url can legitimately appear on more than one session (each
      // keeps its own chip), so the url alone is not the key. A per-slot inner
      // set avoids composing a literal separator into one flat key.
      let slotSeen = seen.get(slot.key)
      if (!slotSeen) { slotSeen = new Set<string>(); seen.set(slot.key, slotSeen) }
      if (slotSeen.has(link.identity)) continue
      slotSeen.add(link.identity)
      out.push({ slotKey: slot.key, identity: link.identity, url: link.url, expect })
    }
  }
  return out
}

export default function RemoveMergedCrChips() {
  const { t: i18nT } = useTranslation()
  const connected = useConnected()
  const store = useAppStore()
  const queryClient = useQueryClient()
  // Two-step confirm, matching the per-session unlink: the first select stages
  // (the row says "remove merged — confirm?"), a second commits the sweep. A
  // cross-session durable removal should not fire on a single click.
  const [confirming, setConfirming] = useState(false)
  // How far the sweep has got, for its running "Removing 2 of 5" label. Local
  // because it is only meaningful while the running menu is mounted; the
  // retained outcome below is what survives a dismissal.
  const [progress, setProgress] = useState<SweepProgress>({ done: 0, total: 0 })
  const errorId = useId()

  const sweep = useMutation({
    mutationKey: SWEEP_KEY,
    mutationFn: async () => {
      setProgress({ done: 0, total: 0 })
      const candidates = collectCandidates(store.getState().dashboard.slots)
      if (candidates.length === 0) return
      // Ask the provider for each change's CURRENT state. De-dupe urls: the same
      // change can be chipped on several sessions, and the status endpoint keys
      // its answer by url. Send in chunks the server will not truncate, and
      // merge the replies, so a change past the server's per-request cap is
      // still answered (and removed) rather than silently skipped forever.
      const urls = Array.from(new Set(candidates.map(c => c.url)))
      const statuses: Record<string, PullRequestStatus> = {}
      for (const group of chunk(urls, STATUS_URLS_PER_REQUEST)) {
        const batch = await api.pullRequestStatuses(group)
        Object.assign(statuses, batch.statuses ?? {})
      }
      // Remove ONLY the chips the provider reports `merged` right now. A url with
      // no status yet (never polled) or any non-merged state is left in place —
      // the guard against removing a chip for a change that has not merged.
      const merged = candidates.filter(c => statuses[c.url]?.state === 'merged')
      setProgress({ done: 0, total: merged.length })
      let done = 0
      for (const c of merged) {
        // Re-check the session generation right before each removal: if the
        // session behind the key was recreated or rebound since the sweep
        // started, its live generation no longer matches `c.expect` and the
        // removal would target the wrong session — skip it (the backend would
        // 409 it anyway; skipping avoids a guaranteed-failed request).
        const live = store.getState().dashboard.slots.find(s => s.key === c.slotKey)
        if (!live || slotGeneration(live) !== c.expect) {
          done += 1
          setProgress({ done, total: merged.length })
          continue
        }
        await api.unlinkSourceLink(c.slotKey, c.identity, c.expect)
        // The DELETE is an await: the slot behind `c.slotKey` may have been
        // recreated or rebound while it was in flight, and a same-key
        // replacement can carry the SAME change identity. Patching the cached
        // list by identity across the slot-key prefix would then strip the
        // replacement's still-valid link. So filter the cache ONLY when the
        // live generation still equals the one we targeted; invalidation stays
        // unconditional so the replacement's own list reconciles either way.
        // Mirrors the `patchable` guard in SourceLinksSubmenu's onSuccess.
        const after = store.getState().dashboard.slots.find(s => s.key === c.slotKey)
        if (after && slotGeneration(after) === c.expect) {
          queryClient.setQueriesData<NonNullable<ChatSlot['source_links']>>(
            { queryKey: ['session-source-links', c.slotKey] },
            prev => prev?.filter(link => link.identity !== c.identity),
          )
        }
        void queryClient.invalidateQueries({ queryKey: ['session-source-links', c.slotKey] })
        done += 1
        setProgress({ done, total: merged.length })
      }
    },
  })

  // Pending + the latest error read from the MUTATION cache, not component
  // state, so a sweep that is running or failed while the menu was dismissed is
  // recovered when it reopens.
  const busy = useMutationState({ filters: { mutationKey: SWEEP_KEY, status: 'pending' }, select: () => true }).length > 0
  const errors = useMutationState({ filters: { mutationKey: SWEEP_KEY }, select: m => m.state.error })
  const error = busy ? null : errors.at(-1) ?? null
  // The provider-status endpoint is OWNER-ONLY (403 for a non-owner dashboard
  // user), while the slots payload still sends chips to such a user, so the item
  // renders for them but its first request is refused. Name that refusal
  // specifically — it is not a retryable "stopped", it is "this is owner-only"
  // — mirroring resumeAll's owner-only notice. Exclude the gateway's
  // session-expiry 403 (`X-Auth-Required: true`, surfaced as `authRequired`):
  // that one hits the owner too when their cookie lapses mid-session, and a
  // re-auth recovers it, so it must fall through to the generic "stopped"
  // notice whose hand-off text carries the real "sign in again" reason — never
  // be mislabelled "owner only". The owner-only refusal itself carries no
  // `authRequired`, so this keeps naming it while releasing the expiry case.
  const ownerOnly =
    error != null &&
    (error as { status?: number }).status === 403 &&
    (error as { authRequired?: boolean }).authRequired !== true

  // Nothing to sweep when offline (the removal endpoint is unreachable) or when
  // no session carries an unlinkable change chip at all.
  const anyCandidate = collectCandidates(store.getState().dashboard.slots).length > 0
  if (!anyCandidate) return null

  const disabled = !connected || busy

  return (
    <>
    {/* Separator lives WITH the item so both appear or neither does — the host
        menu need not know whether this action has anything to offer. */}
    <DropdownMenuSeparator />
    <DropdownMenuItem
      data-remove-merged-cr-chips
      data-confirming={confirming || undefined}
      data-testid="remove-merged-cr-chips"
      disabled={disabled}
      onSelect={event => {
        // Keep the menu open through staging, the running sweep, and any error,
        // so the two-step confirm and the progress label stay visible.
        event.preventDefault()
        if (disabled) return
        if (confirming) {
          setConfirming(false)
          sweep.mutate()
        } else {
          // Drop any retained outcome from a PRIOR sweep so a new staging starts
          // clean. `sweep.reset()` only resets this hook's observer; the settled
          // mutation lingers in the shared MutationCache until gc, and
          // `useMutationState` above still reads its error from there — so a
          // stale "Stopped" notice would reappear on reopen. Remove the settled
          // sweep mutations from the cache outright.
          sweep.reset()
          const cache = queryClient.getMutationCache()
          for (const m of cache.findAll({ mutationKey: SWEEP_KEY })) {
            if (m.state.status !== 'pending') cache.remove(m)
          }
          setConfirming(true)
        }
      }}
    >
      {busy
        ? <Loader2 className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
        : <GitMerge className="lucide-inline shrink-0 text-muted" aria-hidden="true" />}
      {/* The confirm emphasis rides the label text, not the item: a color
          className on the DropdownMenuItem would restyle a ui/ primitive's own
          colour (restyle ratchet). The data-confirming attribute above is what
          tests and the menu key off. */}
      <span className={`break-words${confirming ? ' text-danger' : ''}`}>
        {busy
          // Until the merged set is known (the status requests are still in
          // flight, or the menu was reopened mid-sweep so `progress` is 0/0) a
          // count would read "1 of 0"; show a plain working label instead, and
          // switch to the "N of M" progress only once a real total is in hand.
          ? progress.total > 0
            ? i18nT('pages.chatSidebar.remove_merged_cr_chips_progress', {
              done: Math.min(progress.done + 1, progress.total),
              n: progress.total,
            })
            : i18nT('pages.chatSidebar.remove_merged_cr_chips_working')
          : confirming
            ? i18nT('pages.chatSidebar.remove_merged_cr_chips_confirm')
            : i18nT('pages.chatSidebar.remove_merged_cr_chips')}
      </span>
    </DropdownMenuItem>
    {/* A stopped sweep surfaces through ErrorNotice + its menu-compatible agent
        hand-off, never a bare text-danger span (the blocking
        `errors-use-error-notice` rule). Mirrors the per-session unlink's error
        surface in SourceLinksSubmenu. */}
    {error != null && !busy && !confirming && (() => {
      const raw = error instanceof Error ? error.message : String(error)
      // Owner-only refusal gets its own MESSAGE (not a retryable "stopped"), but
      // the agent hand-off stays: the blocking `errors-use-error-notice` rule
      // requires one of its sanctioned hand-off decisions on every error notice,
      // and "the agent can't help here" is not one of them. The hand-off is
      // harmless — it opens a chat carrying the refusal text.
      return (
        <>
          <div className="px-3 py-1.5">
            <ErrorNotice
              id={errorId}
              message={ownerOnly
                ? i18nT('pages.chatSidebar.remove_merged_cr_chips_owner_only')
                : i18nT('pages.chatSidebar.remove_merged_cr_chips_failed')}
              report={findReport(raw)}
              testId="remove-merged-cr-chips-error"
            />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={raw} describedBy={errorId} />
        </>
      )
    })()}
    </>
  )
}
