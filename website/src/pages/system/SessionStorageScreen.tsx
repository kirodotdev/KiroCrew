/**
 * Session storage — inventory list (Windows "Installed apps" model).
 *
 * One row per session, expandable to lazy-loaded detail, with search/sort,
 * checkbox bulk select, and a Trash section below.
 *
 * A session is ONE unit here. It happens to be written in two places on disk;
 * that is an implementation detail and the report carries no per-store
 * breakdown, so this screen cannot accidentally surface it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronLeft, ChevronRight, Info, Loader2, Search } from 'lucide-react'
import { api } from '../../api/client'
import Clickable from '../../components/Clickable'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn, ContentSkeleton } from '../../components/ui'
import { compareText, fmtBytes, fmtNumber, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type {
  SessionInventoryDetail,
  SessionInventoryItem,
  SessionInventoryList,
  SessionStorageBatch,
  SessionStorageCleanup,
  SessionStorageEmptyJob,
  SessionTrashRefusal,
} from '../../types'

type SortKey = 'largest' | 'oldest' | 'name'

/**
 * How long after arming a confirm click is ignored.
 *
 * Longer than a platform double-click interval (500ms on macOS at the slowest
 * setting), so the second half of a double-click on the arm button can never be
 * received as consent to delete.
 */
const CONFIRM_ARM_MS = 600

/**
 * Rows per page.
 *
 * The list is a full inventory: an install that has been running for months holds
 * hundreds of conversations, and the replay-only group is capped at 200 rows on the
 * server. Rendering all of them put the Trash section — the one place a staged
 * delete is confirmed or undone — thousands of pixels below the fold, so the
 * screen's own footer was unreachable without scrolling past the entire store.
 *
 * Twenty is a little under two viewport-heights of rows at the shipped row height,
 * which keeps the pager, the sweep control and the Trash reachable on one screen
 * while still showing enough rows that "biggest first" answers the question the
 * screen exists for without paging at all.
 */
const PAGE_SIZE = 20

export default function SessionStorageScreen({ onBack }: { onBack: () => void }) {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<SortKey>('largest')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [trashOpen, setTrashOpen] = useState(true)
  const [refused, setRefused] = useState<SessionTrashRefusal[]>([])
  // One page cursor per list. They are independent because the replay-only group is
  // collapsed by default: paging the main list must not move a reader's place inside
  // the group they had open, and vice versa.
  const [page, setPage] = useState(1)
  const [bgPage, setBgPage] = useState(1)

  // Arming state for destructive actions (same two-guard pattern as before)
  const [arming, setArming] = useState<string | null>(null)
  const [armedAt, setArmedAt] = useState(0)
  const arm = (id: string | null) => {
    setArming(id)
    setArmedAt(id === null ? 0 : Date.now())
  }

  const { data, isLoading } = useQuery<SessionInventoryList>({
    queryKey: ['session-inventory'],
    queryFn: api.sessionInventory,
    refetchOnWindowFocus: false,
    // The shared client sets staleTime: Infinity — freshness there comes from
    // WebSocket invalidation, and nothing pushes an event for a delete that
    // finished while this screen was unmounted. Without this, leaving the page
    // during an empty and coming back re-rendered the CACHED pre-delete list: the
    // batch still listed, the totals unchanged. staleTime 0 alone is enough (a stale
    // query refetches when it mounts); `refetchOnMount: 'always'` on top of it was
    // two spellings of one rule. The one scan on mount is the price of never showing
    // a store that is already gone — a WebSocket event on storage mutations would let
    // this go back to being cache-backed, and is the better long-term shape.
    staleTime: 0,
  })

  /**
   * The running or last-finished empty.
   *
   * Polled rather than awaited on the mutation, because the delete outlives the
   * request that starts it and outlives this component: a job started here and
   * still running when the user comes back is picked up by this same query, which
   * is what makes "you can leave the page" true on screen and not just in the
   * gateway. The endpoint reads counters and touches no store, so a 1s poll costs
   * nothing — but it only polls while something is running.
   */
  const { data: emptyState } = useQuery<{ job: SessionStorageEmptyJob | null }>({
    queryKey: ['session-empty-job'],
    queryFn: api.sessionStorageEmptyStatus,
    // staleTime 0 alone, same as the inventory query above: a stale query already
    // refetches when it mounts, so pairing it with `refetchOnMount: 'always'` is two
    // spellings of one rule.
    staleTime: 0,
    refetchInterval: q => {
      const job = q.state.data?.job
      if (!job) return false
      // Slowly, not never, once it settles. The server retires a finished job on
      // its own clock; a client that stopped at `running: false` would never see
      // that happen, so the outcome line would sit on an open tab indefinitely -
      // the same "presented as current long after the fact" this screen is fixing,
      // moved from the gateway into one session.
      return job.running ? 1_000 : 30_000
    },
  })
  const emptyJob = emptyState?.job ?? null
  const emptyRunning = emptyJob !== null && emptyJob.running

  const invalidate = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['session-inventory'] })
    setSelected(new Set())
  }, [qc])

  const trashMut = useMutation({
    mutationFn: (uids: string[]) => api.sessionInventoryTrash(uids),
    onSuccess: (result) => {
      if (result.refused.length > 0) setRefused(result.refused)
      else setRefused([])
      invalidate()
    },
  })

  const restoreMut = useMutation({
    mutationFn: (batchId: string) => api.sessionStorageRestore(batchId),
    onSuccess: invalidate,
  })

  /**
   * Starts an empty. It does NOT wait for the files to go.
   *
   * The response only says the work was accepted, so there is nothing to
   * invalidate here yet — the inventory is still true until the delete lands. What
   * this does is disarm the confirm and kick the job poll, which then owns
   * reporting the run and refreshing the list when it finishes.
   *
   * On SETTLED, not on success: a second tab that clicks while one empty is running
   * gets a 409, which react-query treats as an error - so a success-only hook left
   * that tab with no poll and no progress, which is the exact "nothing is happening"
   * this screen exists to remove. The 409 means a job EXISTS, so it is precisely the
   * case that must go and read it.
   */
  const emptyMut = useMutation({
    mutationFn: (batchId: string) => api.sessionStorageEmpty([batchId]),
    onSettled: () => {
      setArming(null)
      void qc.invalidateQueries({ queryKey: ['session-empty-job'] })
    },
  })

  // A finished job means the store on disk no longer matches the listing in hand,
  // so the scan is re-run exactly once per job rather than on a timer. Keyed on the
  // job id as well as the flag: two empties in a row must each trigger their own
  // refresh, and a remount that finds an already-finished job must not re-fire for
  // one it has already accounted for.
  const settledJob = useRef<string>('')
  useEffect(() => {
    if (!emptyJob || emptyJob.running) return
    if (settledJob.current === emptyJob.job_id) return
    settledJob.current = emptyJob.job_id
    invalidate()
  }, [emptyJob, invalidate])

  const busy = trashMut.isPending || restoreMut.isPending || emptyMut.isPending || emptyRunning
  const blocked = (data?.reclaim_blocked_reason ?? '') !== ''

  // Split sessions: foreground vs background
  const { foreground, backgroundGroup } = useMemo(() => {
    if (!data) return { foreground: [], backgroundGroup: [] }
    const fg: SessionInventoryItem[] = []
    const bg: SessionInventoryItem[] = []
    for (const s of data.sessions) {
      if (s.background) bg.push(s)
      else fg.push(s)
    }
    return { foreground: fg, backgroundGroup: bg }
  }, [data])

  // Filter + sort foreground sessions
  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim()
    let list = foreground
    if (q) {
      list = list.filter(s =>
        s.title.toLowerCase().includes(q) || s.origin.toLowerCase().includes(q),
      )
    }
    const sorted = [...list]
    switch (sort) {
      case 'largest': sorted.sort((a, b) => b.bytes - a.bytes); break
      case 'oldest': sorted.sort((a, b) => a.mtime - b.mtime); break
      case 'name': sorted.sort((a, b) => compareText(a.title || a.origin, b.title || b.origin)); break
    }
    return sorted
  }, [foreground, search, sort])

  // A new query or a new sort makes the old page number meaningless — page 7 of a
  // three-page result is an empty screen — so the cursor goes back to the start.
  useEffect(() => { setPage(1) }, [search, sort])

  /**
   * The visible slice, and the page the reader is actually on.
   *
   * `page` is clamped rather than trusted: a delete or a sweep can shrink the list
   * under a cursor that is already past the end, and clamping here keeps the rows
   * and the pager label reading from the same number instead of showing "page 9 of
   * 4" over an empty list.
   */
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage = Math.min(page, pages)
  const pageStart = (safePage - 1) * PAGE_SIZE
  const visible = useMemo(
    () => filtered.slice(pageStart, pageStart + PAGE_SIZE),
    [filtered, pageStart],
  )

  // The replay-only group's true size and total come from the server. The rows in
  // `data.sessions` are only its largest members — deriving the group from them
  // would under-report by six figures on the installs this screen exists for.
  const bgSummary = data?.background
  const bgExpanded = expanded.has('__background__')
  const bgNotListed = Math.max(0, (bgSummary?.sessions ?? 0) - backgroundGroup.length)
  const bgPages = Math.max(1, Math.ceil(backgroundGroup.length / PAGE_SIZE))
  const bgSafePage = Math.min(bgPage, bgPages)
  const bgStart = (bgSafePage - 1) * PAGE_SIZE
  const bgVisible = useMemo(
    () => backgroundGroup.slice(bgStart, bgStart + PAGE_SIZE),
    [backgroundGroup, bgStart],
  )
  // Whether the age sweep is actually on screen. The truncation note tells the
  // reader where to reclaim the rest, so it must not point at a control that is
  // hidden — which it is when reclaiming is refused, or when no threshold has
  // anything to take.
  const sweepShown =
    !blocked && (data?.age_options ?? []).some(o => o.sessions > 0)

  /**
   * The largest row, which scales every bar.
   *
   * Reduced rather than `Math.max(...rows)`: spreading an array becomes that many
   * function arguments, and the measured machine this screen exists for holds over
   * 166,000 sessions — far past the engine's argument limit, so the spread form
   * throws `RangeError` and blanks the screen on exactly the install that needs it.
   */
  const maxBytes = useMemo(() => {
    if (!data) return 1
    return data.sessions.reduce((max, s) => (s.bytes > max ? s.bytes : max), 1)
  }, [data])

  /**
   * A refused row's label.
   *
   * Resolved from the listing rather than printing the raw uid: an id is only
   * loosely constrained server-side, so it is not a string to render, and the
   * title/origin the server already scrubbed says more to a reader anyway. The
   * uid is kept as the action handle, never as display text.
   */
  const labelFor = useCallback(
    (uid: string) => {
      const row = data?.sessions.find(s => s.uid === uid)
      return row ? row.title || row.origin : i18nT('pages.sessionStorage.unknown_session')
    },
    [data],
  )

  // Selection helpers
  const toggleSelect = (uid: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })
  }
  const toggleExpand = (uid: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })
  }
  const clearSelection = () => setSelected(new Set())
  const selectedBytes = useMemo(() => {
    if (!data) return 0
    return data.sessions
      .filter(s => selected.has(s.uid))
      .reduce((sum, s) => sum + s.bytes, 0)
  }, [data, selected])

  const handleBulkTrash = () => {
    const uids = [...selected].filter(uid => {
      const s = data?.sessions.find(x => x.uid === uid)
      return s && !s.active
    })
    if (uids.length > 0) trashMut.mutate(uids)
  }

  /** A confirm that arrives within the double-click window is not consent. */
  const onConfirmEmpty = (batchId: string) => {
    if (Date.now() - armedAt < CONFIRM_ARM_MS) return
    emptyMut.mutate(batchId)
  }

  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        onClick={onBack}
        className="self-start flex items-center gap-1 text-[11.5px] text-muted hover:text-text transition-colors"
      >
        <ChevronLeft className="w-3.5 h-3.5" />
        <span>{i18nT('pages.sessionStorage.back_to_disk')}</span>
      </button>

      {isLoading || !data ? (
        <ContentSkeleton rows={8} />
      ) : (
        <>
          {/* Header */}
          <div>
            <h1 className="text-lg font-semibold text-text-strong">
              {i18nT('pages.sessionStorage.heading')}
            </h1>
            <p className="text-[12px] text-muted mt-0.5">
              {i18nT('pages.sessionStorage.subheading', {
                sessions: fmtNumber(data.total_sessions),
                total: fmtBytes(data.total_bytes),
                reclaimable: fmtBytes(data.reclaimable_bytes),
              })}
            </p>
          </div>

          {/* Blocked reason */}
          {blocked && (
            <div className="flex items-start gap-2 text-[11.5px] text-warn">
              <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{data.reclaim_blocked_reason}</span>
            </div>
          )}

          {/* Why the controls above and below are dead, said WHERE they are. The run
              takes minutes on a large store and the "Emptying Trash" progress line
              lives down in the Trash section, past a long list - so a user arriving
              mid-run to trash a session met greyed buttons with the explanation
              offscreen. Same defect as the one this PR fixes, one level up: the screen
              knew something the user could not see. */}
          {emptyRunning && (
            <div
              role="status"
              className="flex items-center gap-2 text-xs text-muted bg-bg-elevated border border-border rounded-md px-2.5 py-1.5"
            >
              <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
              <span>{i18nT('pages.sessionStorage.busy_emptying')}</span>
            </div>
          )}

          {/* Bulk reclaim by age — the only path that reaches the sessions the
              list does not name individually. Hidden when reclaiming is refused
              outright, so the screen never offers an action that can only fail.
              `?? []` because this arrives over the wire: a tab left open across a
              gateway upgrade would otherwise crash the whole screen on a field
              the older build did not send. */}
          {!blocked && (
            <ReclaimByAge options={data.age_options ?? []} busy={busy} onDone={invalidate} />
          )}

          {/* Toolbar: search + sort */}
          <div className="flex items-center gap-2">
            <label className="flex-1 flex items-center gap-2 bg-bg-elevated border border-border focus-within:border-accent rounded-md px-2.5 py-1.5">
              <Search className="w-3.5 h-3.5 text-muted" />
              {/* The wrapping label carries only the magnifier glyph, so the
                  placeholder is this field's only visible name — and a
                  placeholder is not an accessible name. One string for both, so
                  the two can never end up describing different fields. */}
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                aria-label={i18nT('pages.sessionStorage.search_placeholder')}
                placeholder={i18nT('pages.sessionStorage.search_placeholder')}
                className="flex-1 bg-transparent border-0 text-[13px] text-text placeholder:text-muted"
              />
            </label>
            {/* SimpleSelect, not a native <select>: a native popup is drawn by the
                OS and ignores the theme. */}
            <SimpleSelect
              options={['largest', 'oldest', 'name']}
              optionLabels={[
                i18nT('pages.sessionStorage.sort_largest'),
                i18nT('pages.sessionStorage.sort_oldest'),
                i18nT('pages.sessionStorage.sort_name'),
              ]}
              value={sort}
              onChange={value => setSort(value as SortKey)}
            />
          </div>

          {/* Bulk selection strip */}
          {selected.size > 0 && (
            <div className="flex items-center gap-3 bg-bg-elevated border border-border-strong rounded-md px-3 py-2 text-[12px]">
              <span className="font-medium text-text-strong">
                {i18nT('pages.sessionStorage.bulk_selected', { count: fmtNumber(selected.size) })}
              </span>
              <span className="text-muted font-mono tabular-nums">{fmtBytes(selectedBytes)}</span>
              <span className="flex-1" />
              <button
                type="button"
                onClick={clearSelection}
                className="text-muted underline underline-offset-2 decoration-border-strong hover:text-text text-[12px] bg-transparent border-0 cursor-pointer"
              >
                {i18nT('pages.sessionStorage.clear_selection')}
              </button>
              {!blocked && (
                <Btn danger disabled={busy} onClick={handleBulkTrash}>
                  {i18nT('pages.sessionStorage.move_to_trash_bulk')}
                </Btn>
              )}
            </div>
          )}

          {/* Refused notice */}
          {refused.length > 0 && (
            <div className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[12px]">
              <div className="font-medium text-text-strong mb-1">
                {i18nT('pages.sessionStorage.refused_heading', { count: fmtNumber(refused.length) })}
              </div>
              {refused.map(r => (
                <div key={r.uid} className="text-muted">
                  {labelFor(r.uid)}: {refusalReason(r.reason)}
                </div>
              ))}
            </div>
          )}

          {/* Session list */}
          <div className="border-t border-border">
            <Pager
              page={safePage}
              pages={pages}
              total={filtered.length}
              from={filtered.length === 0 ? 0 : pageStart + 1}
              to={Math.min(pageStart + PAGE_SIZE, filtered.length)}
              onPage={setPage}
            />
            {visible.map(session => (
              <SessionRow
                key={session.uid}
                session={session}
                maxBytes={maxBytes}
                isSelected={selected.has(session.uid)}
                isExpanded={expanded.has(session.uid)}
                onToggleSelect={() => toggleSelect(session.uid)}
                onToggleExpand={() => toggleExpand(session.uid)}
                blocked={blocked}
                busy={busy}
                onTrash={() => trashMut.mutate([session.uid])}
              />
            ))}

            {/* A search that matches nothing rendered as a blank gap between the
                toolbar and the Trash, which reads as a screen that failed to load
                rather than a query with no hits.

                Scoped to CONVERSATIONS on purpose: the filter covers this list only,
                and the replay-only group below renders unfiltered. A line claiming
                "no sessions match" would be false on screen the moment a search hit
                one of the group's rows, and would stop a reader who had found what
                they were looking for. */}
            {search.trim() !== '' && filtered.length === 0 && (
              <p className="text-[12px] text-muted px-1.5 py-3 border-b border-border">
                {i18nT('pages.sessionStorage.no_matches')}
              </p>
            )}

            {/* Background agents group */}
            {backgroundGroup.length > 0 && bgSummary && (
              <div className="border-b border-border">
                <Clickable
                  className="flex items-center gap-2.5 px-1.5 py-2 cursor-pointer hover:bg-bg-hover"
                  onClick={() => toggleExpand('__background__')}
                  aria-expanded={bgExpanded}
                >
                  <ChevronRight className={`w-3.5 h-3.5 text-muted transition-transform ${bgExpanded ? 'rotate-90' : ''}`} />
                  <span className="text-[13px] font-medium text-text-strong">
                    {i18nT('pages.sessionStorage.background_group', { count: fmtNumber(bgSummary.sessions) })}
                  </span>
                  <span className="text-[12px] text-muted font-mono tabular-nums">{fmtBytes(bgSummary.bytes)}</span>
                </Clickable>
                {bgExpanded && (
                  <div className="pl-4">
                    {bgNotListed > 0 && (
                      <p className="text-[11.5px] text-muted px-1.5 pb-2">
                        {i18nT(
                          sweepShown
                            ? 'pages.sessionStorage.background_truncated_sweep'
                            : 'pages.sessionStorage.background_truncated',
                          {
                            listed: fmtNumber(backgroundGroup.length),
                            total: fmtNumber(bgSummary.sessions),
                          },
                        )}
                      </p>
                    )}
                    {backgroundGroup.length > PAGE_SIZE && (
                      <Pager
                        page={bgSafePage}
                        pages={bgPages}
                        total={backgroundGroup.length}
                        from={bgStart + 1}
                        to={Math.min(bgStart + PAGE_SIZE, backgroundGroup.length)}
                        onPage={setBgPage}
                      />
                    )}
                    {bgVisible.map(session => (
                      <SessionRow
                        key={session.uid}
                        session={session}
                        maxBytes={maxBytes}
                        isSelected={selected.has(session.uid)}
                        isExpanded={expanded.has(session.uid)}
                        onToggleSelect={() => toggleSelect(session.uid)}
                        onToggleExpand={() => toggleExpand(session.uid)}
                        blocked={blocked}
                        busy={busy}
                        onTrash={() => trashMut.mutate([session.uid])}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Trash section */}
          <TrashSection
            trash={data.trash}
            busy={busy}
            job={emptyJob}
            startFailed={emptyMut.isError && emptyJob === null}
            trashOpen={trashOpen}
            onToggleTrash={() => setTrashOpen(!trashOpen)}
            arming={arming}
            onArm={arm}
            onRestore={id => restoreMut.mutate(id)}
            onConfirmEmpty={onConfirmEmpty}
          />
        </>
      )}
    </div>
  )
}

/* ─────────────────── Reclaim by age ─────────────────── */

/**
 * Bulk reclaim by last-used age.
 *
 * The per-row checkboxes cannot reach the bulk of a large store: the replay-only
 * group holds six figures of sessions and only its largest are listed. This is
 * the surface that can, and it needs no selection — the server re-derives which
 * sessions a threshold covers at the moment of the move, so the numbers shown
 * here are a preview and never the thing acted upon.
 *
 * A preview is mandatory rather than a convenience. The counts arriving with the
 * listing are seconds old at best, so the confirm step re-asks the server (the
 * endpoint's own `dry_run`) and shows what IT says it would take. Confirming then
 * repeats the same threshold, not the previewed uids.
 */
function ReclaimByAge({
  options, busy, onDone,
}: {
  options: SessionInventoryList['age_options']
  busy: boolean
  onDone: () => void
}) {
  const offered = options.filter(o => o.sessions > 0)
  const [days, setDays] = useState(() => offered[offered.length - 1]?.days ?? 90)
  // The preview carries the threshold it was TAKEN for, so the numbers shown and
  // the sweep the confirm runs are read from the SAME object. That makes "the
  // preview describes the action" structural rather than something to keep in
  // sync — and this is a bulk delete, so the two must never disagree. Changing
  // the threshold clears it, and the selector is locked while one is in flight,
  // so a late response cannot land over a different selection.
  const [preview, setPreview] = useState<{ days: number; result: SessionStorageCleanup } | null>(
    null,
  )

  const previewMut = useMutation({
    mutationFn: (d: number) => api.sessionStorageCleanup(d, true),
    onSuccess: (result, d) => setPreview({ days: d, result }),
  })
  const sweepMut = useMutation({
    mutationFn: (d: number) => api.sessionStorageCleanup(d, false),
    onSuccess: () => { setPreview(null); onDone() },
  })
  const working = busy || previewMut.isPending || sweepMut.isPending
  // A refused cleanup must say so. Without this the button simply re-enables and
  // nothing on screen explains why nothing moved — the same "looks broken, no
  // reason given" symptom this screen exists to remove, at a destructive moment.
  const failed = previewMut.isError || sweepMut.isError

  if (offered.length === 0) return null
  const chosen = offered.find(o => o.days === days) ?? offered[offered.length - 1]

  return (
    <div className="bg-bg-elevated border border-border rounded-md px-3 py-2.5 flex flex-wrap items-center gap-3 text-[12px]">
      <span className="text-text-strong font-medium">
        {i18nT('pages.sessionStorage.reclaim_by_age')}
      </span>
      <SimpleSelect
        options={offered.map(o => String(o.days))}
        optionLabels={offered.map(o =>
          // `count` is the pluralising variable i18next selects the form on, so the
          // option never reads "1 sessions". `days` and `size` are plain
          // interpolations; only the session count varies in number here, since
          // every threshold offered is 7 days or more.
          i18nT('pages.sessionStorage.age_option', {
            count: o.sessions,
            days: fmtNumber(o.days),
            size: fmtBytes(o.bytes),
          }),
        )}
        value={String(chosen.days)}
        onChange={value => { setDays(Number(value)); setPreview(null) }}
        disabled={working}
        aria-label={i18nT('pages.sessionStorage.reclaim_by_age')}
      />
      <span className="flex-1" />
      {failed && (
        <span className="text-danger">{i18nT('pages.sessionStorage.sweep_failed')}</span>
      )}
      {preview === null ? (
        <Btn disabled={working} onClick={() => previewMut.mutate(chosen.days)}>
          {i18nT('pages.sessionStorage.preview_sweep')}
        </Btn>
      ) : (
        <>
          <span className="text-muted">
            {i18nT('pages.sessionStorage.sweep_preview', {
              count: preview.result.sessions,
              size: fmtBytes(preview.result.bytes),
            })}
            {/* Above the per-batch cap the preview is NOT the whole job, so say
                that a repeat sweep is needed rather than letting the number read
                as the total. */}
            {preview.result.remaining > 0 && (
              <> {i18nT('pages.sessionStorage.sweep_remaining', {
                remaining: fmtNumber(preview.result.remaining),
              })}</>
            )}
          </span>
          <Btn disabled={working} onClick={() => setPreview(null)}>
            {i18nT('pages.sessionStorage.cancel')}
          </Btn>
          <Btn
            danger
            disabled={working || preview.result.sessions === 0}
            onClick={() => sweepMut.mutate(preview.days)}
          >
            {i18nT('pages.sessionStorage.move_to_trash_bulk')}
          </Btn>
        </>
      )}
    </div>
  )
}

/* ─────────────────── Pager ─────────────────── */

/**
 * Page controls for one list.
 *
 * Rendered ABOVE its rows, not below. Below the last row is the conventional
 * position and the wrong one here: the reason the list is paged at all is that a
 * reader could not reach the end of it, so putting the control that fixes that at
 * the end would only be reachable by the scroll it exists to avoid.
 *
 * The range label carries the totals, so "20 rows" never reads as "20 sessions" —
 * the count of what is NOT on screen is the fact a person deciding what to reclaim
 * needs, and it is the one a bare pager throws away.
 */
function Pager({
  page, pages, total, from, to, onPage,
}: {
  page: number
  pages: number
  total: number
  from: number
  to: number
  onPage: (page: number) => void
}) {
  if (pages <= 1) return null
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 px-1.5 py-2 border-b border-border">
      <span className="text-[11.5px] text-muted tabular-nums">
        {i18nT('pages.sessionStorage.showing_range', {
          from: fmtNumber(from),
          to: fmtNumber(to),
          total: fmtNumber(total),
        })}
      </span>
      <div className="flex items-center gap-2">
        <Btn disabled={page <= 1} onClick={() => onPage(page - 1)}>
          {i18nT('pages.sessionStorage.prev_page')}
        </Btn>
        <span className="text-[12px] text-muted tabular-nums">
          {i18nT('pages.sessionStorage.page_of', {
            page: fmtNumber(page),
            pages: fmtNumber(pages),
          })}
        </span>
        <Btn disabled={page >= pages} onClick={() => onPage(page + 1)}>
          {i18nT('pages.sessionStorage.next_page')}
        </Btn>
      </div>
    </div>
  )
}

/* ─────────────────── Session Row ─────────────────── */

function SessionRow({
  session, maxBytes, isSelected, isExpanded,
  onToggleSelect, onToggleExpand, blocked, busy, onTrash,
}: {
  session: SessionInventoryItem
  maxBytes: number
  isSelected: boolean
  isExpanded: boolean
  onToggleSelect: () => void
  onToggleExpand: () => void
  blocked: boolean
  busy: boolean
  onTrash: () => void
}) {
  const title = session.title || session.origin
  // The row's visible title is the selection checkbox's label. Bound by
  // `aria-labelledby` rather than by turning the title into a `<label>`: a
  // label would make clicking the title toggle selection instead of expanding
  // the row. `uid` is unique across both lists (they partition on `background`),
  // so the reference cannot collide.
  const titleId = `session-title-${session.uid}`

  return (
    <div className={`border-b border-border ${isExpanded ? 'bg-bg-hover' : ''}`}>
      <Clickable
        onClick={onToggleExpand}
        aria-expanded={isExpanded}
        className="cursor-pointer hover:bg-bg-hover"
        style={{ display: 'grid', gridTemplateColumns: '20px minmax(0, 1fr) 100px 66px 18px', gap: '10px', alignItems: 'center', padding: '8px 6px' }}
      >
        {/* Disabled with a reason attached. A greyed checkbox and no explanation
            reads as a broken screen — the reason is already known here, and the
            badge beside the title is easy to miss on a long list. */}
        <input
          type="checkbox"
          checked={isSelected}
          aria-labelledby={titleId}
          disabled={session.active}
          title={
            session.active
              ? session.live
                ? i18nT('pages.sessionStorage.cannot_delete_running')
                : i18nT('pages.sessionStorage.cannot_delete_resumable')
              : undefined
          }
          onChange={onToggleSelect}
          onClick={e => e.stopPropagation()}
          className="w-[13px] h-[13px] accent-muted cursor-pointer disabled:opacity-35 disabled:cursor-default"
        />
        <div className="min-w-0">
          <div id={titleId} className="text-[13px] text-text-strong truncate">
            {title}
            {session.active && (
              <span className="ml-2 inline-block px-1.5 border border-border-strong rounded text-[10px] text-muted align-middle">
                {session.live
                  ? i18nT('pages.sessionStorage.in_use')
                  : i18nT('pages.sessionStorage.resumable')}
              </span>
            )}
          </div>
          {session.title !== '' && (
            <div className="text-[11.5px] text-muted truncate mt-0.5">{session.origin}</div>
          )}
        </div>
        <div className="text-right">
          <div className="text-[12.5px] text-text font-mono tabular-nums">{fmtBytes(session.bytes)}</div>
          <div className="h-[2px] bg-border mt-1 rounded-full overflow-hidden">
            <div className="h-full bg-muted rounded-full" style={{ width: `${Math.max(1, (session.bytes / maxBytes) * 100)}%` }} />
          </div>
        </div>
        <div className="text-right text-[11.5px] text-muted">
          {fmtRelative(session.mtime)}
        </div>
        <div className="text-center text-muted cursor-pointer">
          <ChevronDown className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
        </div>
      </Clickable>
      {isExpanded && (
        <SessionDetail
          uid={session.uid}
          active={session.active}
          live={session.live}
          blocked={blocked}
          busy={busy}
          onTrash={onTrash}
        />
      )}
    </div>
  )
}

/* ─────────────────── Lazy detail ─────────────────── */

function SessionDetail({
  uid, active, live, blocked, busy, onTrash,
}: {
  uid: string
  active: boolean
  live: boolean
  blocked: boolean
  busy: boolean
  onTrash: () => void
}) {
  const { data, isLoading } = useQuery<SessionInventoryDetail>({
    queryKey: ['session-detail', uid],
    queryFn: () => api.sessionInventoryDetail(uid),
  })

  if (isLoading || !data) {
    return <div className="px-10 pb-3"><ContentSkeleton rows={3} /></div>
  }

  return (
    <div className="px-10 pb-3">
      {data.first_message && (
        <p className="text-[12.5px] text-text mb-3 pl-2.5 border-l-2 border-border-strong italic">
          &ldquo;{data.first_message}&rdquo;
        </p>
      )}
      <div className="flex gap-6 mb-3">
        <Fact label={i18nT('pages.sessionStorage.detail_size')} value={fmtBytes(data.bytes)} />
        <Fact label={i18nT('pages.sessionStorage.detail_turns')} value={fmtNumber(data.turns)} />
        <Fact label={i18nT('pages.sessionStorage.detail_images')} value={fmtNumber(data.images)} />
        <Fact label={i18nT('pages.sessionStorage.detail_last_used')} value={fmtRelative(data.mtime)} />
      </div>
      {!active && !blocked && (
        <button
          type="button"
          disabled={busy}
          onClick={onTrash}
          className="text-[12px] text-danger underline underline-offset-2 decoration-border-strong hover:decoration-danger bg-transparent border-0 cursor-pointer disabled:opacity-35"
        >
          {i18nT('pages.sessionStorage.move_to_trash_single')}
        </button>
      )}
      {active && (
        <span className="text-[11.5px] text-muted">
          {live
            ? i18nT('pages.sessionStorage.cannot_delete_running')
            : i18nT('pages.sessionStorage.cannot_delete_resumable')}
        </span>
      )}
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wide text-muted">{label}</div>
      <div className="text-[13px] text-text-strong font-mono tabular-nums mt-0.5">{value}</div>
    </div>
  )
}

/* ─────────────────── Trash Section ─────────────────── */

function TrashSection({
  trash, busy, job, startFailed, trashOpen, onToggleTrash,
  arming, onArm, onRestore, onConfirmEmpty,
}: {
  trash: SessionInventoryList['trash']
  busy: boolean
  job: SessionStorageEmptyJob | null
  startFailed: boolean
  trashOpen: boolean
  onToggleTrash: () => void
  arming: string | null
  onArm: (id: string | null) => void
  onRestore: (id: string) => void
  onConfirmEmpty: (id: string) => void
}) {
  const batches = trash.batches

  return (
    <div className="border-t border-border mt-2 pt-2">
      <Clickable
        className="flex items-center gap-2.5 px-1.5 py-2 cursor-pointer hover:bg-bg-hover"
        onClick={onToggleTrash}
        aria-expanded={trashOpen}
      >
        <ChevronRight className={`w-3.5 h-3.5 text-muted transition-transform ${trashOpen ? 'rotate-90' : ''}`} />
        <span className="text-[13px] font-medium text-text-strong">
          {i18nT('pages.sessionStorage.trash')}
        </span>
        {batches.length > 0 && (
          <span className="text-[12px] text-muted font-mono tabular-nums">
            {i18nT('pages.sessionStorage.trash_summary', {
              sessions: fmtNumber(batches.reduce((s, b) => s + b.sessions, 0)),
              size: fmtBytes(trash.bytes),
            })}
          </span>
        )}
        {batches.length === 0 && !job?.running && (
          <span className="text-[12px] text-muted">{i18nT('pages.sessionStorage.trash_empty')}</span>
        )}
      </Clickable>

      {/* The delete's own status. Outside the collapsed body on purpose: a run
          that takes minutes must stay visible whether or not the section that
          started it is open, and it is the only thing on screen that says the
          work did not die with the click. */}
      {job !== null && <EmptyProgress job={job} />}

      {/* A request that never became a job. `onSettled` disarms the confirm on any
          outcome - which is what lets a 409 tab pick up the running job - so a POST
          that failed cleared the button and left nothing behind: the click read as
          accepted while nothing ran. That is this screen's own reported symptom in a
          corner, so it gets a line of its own.

          Gated on the ABSENCE of a job, not on the mutation's error state: react-query
          keeps `isError` set until the next mutate, so after a 409 (a second tab) the
          poll picks up the real job and, the moment it settled, this line appeared
          above "Freed 18GB" - the screen telling the user the delete both never ran
          and freed 18GB, on the one surface whose whole job is answering whether the
          irreversible thing happened. When a job exists, the job is the answer. */}
      {startFailed && (
        <p className="px-1.5 py-2 text-[12px] text-danger" role="status">
          {i18nT('pages.sessionStorage.empty_not_started')}
        </p>
      )}

      {trashOpen && batches.length > 0 && (
        <div className="pl-4">
          <p className="text-[11.5px] text-muted px-1.5 pb-2">
            {i18nT('pages.sessionStorage.trash_note')}
          </p>
          {batches.map(b => (
            <TrashBatchRow
              key={b.batch_id}
              batch={b}
              busy={busy}
              arming={arming}
              onArm={onArm}
              onRestore={onRestore}
              onConfirmEmpty={onConfirmEmpty}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * What the empty is doing, while it does it.
 *
 * The reported symptom this answers: the three buttons greyed out and nothing
 * else changed, so the only way to learn whether a delete of tens of thousands of
 * sessions had worked was to come back later and see whether the batch was gone.
 *
 * Progress is measured in BYTES against the staged total, not in sessions: the
 * delete walks files, and a session is more than one file, so a session-shaped
 * count would either be a guess or lag the work it claims to describe. The
 * "keeps running" line is load-bearing, not reassurance — the job lives in the
 * gateway, so leaving really is safe, and a user who does not know that waits.
 */
function EmptyProgress({ job }: { job: SessionStorageEmptyJob }) {
  if (job.running) {
    // Only when the staged total is known: a batch whose manifest reported no
    // bytes would otherwise render a bar permanently at 0% next to a number that
    // is climbing, which reads as stuck.
    const pct = job.total_bytes > 0
      ? Math.min(100, (job.freed_bytes / job.total_bytes) * 100)
      : null
    return (
      <div className="px-1.5 py-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          {/* The live region is the STATE, not the counter. The figure below changes
              on every poll, so announcing it makes a screen reader speak roughly
              once a second for a job that runs for minutes; `aria-live="off"`
              leaves it readable without being read out. */}
          <span className="text-[12.5px] text-text-strong" role="status">
            {i18nT('pages.sessionStorage.emptying')}
          </span>
          <span
            className="text-[12px] text-muted font-mono tabular-nums"
            aria-live="off"
          >
            {job.total_bytes > 0
              ? i18nT('pages.sessionStorage.emptying_progress', {
                freed: fmtBytes(job.freed_bytes),
                total: fmtBytes(job.total_bytes),
              })
              : fmtBytes(job.freed_bytes)}
          </span>
          <span className="text-[11.5px] text-muted">
            {i18nT('pages.sessionStorage.emptying_leave_ok')}
          </span>
        </div>
        {pct !== null && (
          <div className="h-[3px] bg-border mt-1.5 rounded-full overflow-hidden">
            <div
              className="h-full bg-accent rounded-full transition-[width] duration-500"
              style={{ width: `${Math.max(1, pct)}%` }}
            />
          </div>
        )}
      </div>
    )
  }
  if (job.error !== '') {
    return (
      <div className="px-1.5 py-2" role="status">
        <p className="text-[12px] text-danger">
          {i18nT('pages.sessionStorage.empty_failed', { freed: fmtBytes(job.freed_bytes) })}
        </p>
        {/* A next step, translated, BEFORE the raw text. The delete stopped partway
            through something irreversible, and the gateway's sentence is English in
            all 12 locales - so in 11 of them the only explanation of a half-finished
            destructive operation was untranslated technical text. This line says what
            to do in the reader's language; the raw sentence stays as the detail. */}
        <p className="text-[11px] text-muted mt-0.5">
          {i18nT('pages.sessionStorage.empty_failed_next_step')}
        </p>
        {/* The gateway's own sentence, on its own line and in a monospace face so it
            reads as the technical detail it is. Deliberately not a clause inside the
            translated line above: it describes an error nobody enumerated, so it
            cannot be a code like the skips are, and splicing English into a localized
            sentence would be the mixed-language result those codes exist to avoid.
            The line above is a whole sentence on its own - a key ending in a
            connector leaves half a sentence outside it, which a translator cannot
            reorder. */}
        <p className="text-[11px] text-muted font-mono mt-0.5">{job.error}</p>
      </div>
    )
  }
  // A kept batch is NOT a success: the user asked for it to be destroyed and it is
  // still in the list above. Rendering "Freed 0 B." here — which is what an
  // outcome read only from `error` produced — is a success-shaped line
  // contradicting the row it sits under, with the reason only in the gateway log.
  if (job.skipped.length > 0) {
    // Deduplicated: two batches kept for the same reason is the same sentence
    // twice. The count is deliberately not shown - it would need a plural form in
    // every locale to say something the reasons already say.
    const reasons = [...new Set(job.skipped)]
    return (
      <p className="px-1.5 py-2 text-[12px] text-warn" role="status">
        {i18nT('pages.sessionStorage.empty_kept', { freed: fmtBytes(job.freed_bytes) })}
        {' '}
        {reasons.map(skipReason).join(' ')}
        {/* Gated on the reason it actually describes. Only the unlisted-file case has
            a file to name, so appending it to every code promised a log line that
            says nothing for the others. */}
        {reasons.includes('unlisted_files') && (
          <> {i18nT('pages.sessionStorage.kept_next_step')}</>
        )}
      </p>
    )
  }
  return (
    <p className="px-1.5 py-2 text-[12px] text-muted" role="status">
      {i18nT('pages.sessionStorage.emptied', { size: fmtBytes(job.freed_bytes) })}
    </p>
  )
}

/**
 * Why a batch was kept, in the user's language.
 *
 * The gateway sends a code rather than prose precisely so this can be translated:
 * interpolating the backend's own sentence would put untranslated internal
 * vocabulary ("staged file is absent from its manifest") in front of a reader.
 */
function skipReason(code: string): string {
  switch (code) {
    case 'unlisted_files': return i18nT('pages.sessionStorage.kept_unlisted_files')
    case 'unreadable_batch': return i18nT('pages.sessionStorage.kept_unreadable')
    case 'outside_trash_root': return i18nT('pages.sessionStorage.kept_outside_root')
    case 'incomplete': return i18nT('pages.sessionStorage.kept_incomplete')
    case 'identity_changed': return i18nT('pages.sessionStorage.kept_identity_changed')
    default: return i18nT('pages.sessionStorage.kept_unknown')
  }
}

function TrashBatchRow({
  batch, busy, arming, onArm, onRestore, onConfirmEmpty,
}: {
  batch: SessionStorageBatch
  busy: boolean
  arming: string | null
  onArm: (id: string | null) => void
  onRestore: (id: string) => void
  onConfirmEmpty: (id: string) => void
}) {
  const isArmed = arming === batch.batch_id

  return (
    <div className="border-b border-border px-1.5 py-2.5 flex flex-wrap items-center gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] text-text-strong">
          {i18nT('pages.sessionStorage.trash_batch_label', {
            sessions: fmtNumber(batch.sessions),
            size: fmtBytes(batch.bytes),
          })}
        </div>
        <div className="text-[11px] text-muted mt-0.5">
          {i18nT('pages.sessionStorage.trash_batch_reason', { reason: batch.reason })}
          {' · '}{fmtRelative(batch.created_at)}
        </div>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={() => onRestore(batch.batch_id)}
        className="text-[12px] text-muted underline underline-offset-2 decoration-border-strong hover:text-text bg-transparent border-0 cursor-pointer disabled:opacity-35"
      >
        {i18nT('pages.sessionStorage.restore')}
      </button>
      {isArmed ? (
        /* Cancel FIRST, so it — not the destructive confirm — occupies the slot
           the arm button just vacated. A fast double-click on "Delete forever"
           would otherwise land its second click on a confirm that appeared
           under the stationary pointer. The time guard below covers the same
           hazard for a keyboard repeat or a re-ordered layout. */
        <>
          <Btn disabled={busy} onClick={() => onArm(null)}>
            {i18nT('pages.sessionStorage.cancel')}
          </Btn>
          <Btn danger disabled={busy} onClick={() => onConfirmEmpty(batch.batch_id)}>
            {i18nT('pages.sessionStorage.confirm_delete_forever')}
          </Btn>
        </>
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => onArm(batch.batch_id)}
          className="text-[12px] text-danger underline underline-offset-2 decoration-border-strong hover:decoration-danger bg-transparent border-0 cursor-pointer disabled:opacity-35"
        >
          {i18nT('pages.sessionStorage.delete_forever')}
        </button>
      )}
    </div>
  )
}

/* ─────────────────── Helpers ─────────────────── */

function refusalReason(reason: string): string {
  switch (reason) {
    case 'in_use': return i18nT('pages.sessionStorage.refused_in_use')
    case 'resumable': return i18nT('pages.sessionStorage.refused_resumable')
    case 'too_fresh': return i18nT('pages.sessionStorage.refused_too_fresh')
    case 'unknown': return i18nT('pages.sessionStorage.refused_unknown')
    default: return reason
  }
}
