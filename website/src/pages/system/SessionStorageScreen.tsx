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
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronLeft, ChevronRight, Info, Search } from 'lucide-react'
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

export default function SessionStorageScreen({ onBack }: { onBack: () => void }) {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<SortKey>('largest')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [trashOpen, setTrashOpen] = useState(true)
  const [refused, setRefused] = useState<SessionTrashRefusal[]>([])

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
  })

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

  const emptyMut = useMutation({
    mutationFn: (batchId: string) => api.sessionStorageEmpty([batchId]),
    onSuccess: () => { setArming(null); invalidate() },
  })

  const busy = trashMut.isPending || restoreMut.isPending || emptyMut.isPending
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

  // The replay-only group's true size and total come from the server. The rows in
  // `data.sessions` are only its largest members — deriving the group from them
  // would under-report by six figures on the installs this screen exists for.
  const bgSummary = data?.background
  const bgExpanded = expanded.has('__background__')
  const bgNotListed = Math.max(0, (bgSummary?.sessions ?? 0) - backgroundGroup.length)
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
            <label className="flex-1 flex items-center gap-2 bg-bg-elevated border border-border rounded-md px-2.5 py-1.5">
              <Search className="w-3.5 h-3.5 text-muted" />
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder={i18nT('pages.sessionStorage.search_placeholder')}
                className="flex-1 bg-transparent border-0 outline-none text-[13px] text-text placeholder:text-muted"
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
            {filtered.map(session => (
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
                    {backgroundGroup.map(session => (
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
          <div className="text-[13px] text-text-strong truncate">
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
  trash, busy, trashOpen, onToggleTrash,
  arming, onArm, onRestore, onConfirmEmpty,
}: {
  trash: SessionInventoryList['trash']
  busy: boolean
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
        {batches.length === 0 && (
          <span className="text-[12px] text-muted">{i18nT('pages.sessionStorage.trash_empty')}</span>
        )}
      </Clickable>

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
