/**
 * Session storage — the control plane behind Performance › Disk.
 *
 * A drill-down, not a fourth plane: Performance answers "how full is this
 * machine", and "what is using it, and can I have it back" is the next question
 * a person asks. The System page deliberately has three planes (its own comment
 * records that App-history was left out rather than added as a fourth), so this
 * gets its own room reached from Disk instead of a tab beside it.
 *
 * A session is ONE unit here. It happens to be written in two places on disk;
 * that is an implementation detail and the report carries no per-store
 * breakdown, so this screen cannot accidentally surface it.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, Info, Trash2, Undo2 } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, ContentSkeleton } from '../../components/ui'
import SegmentedControl, { type Segment } from '../../components/SegmentedControl'
import { fmtBytes, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { SessionStorageBucket, SessionStorageReport } from '../../types'

/** The thresholds the server buckets by, plus an explicit "reclaim nothing". */
type Threshold = '7' | '30' | '90' | 'never'

const THRESHOLD_DAYS: Record<Exclude<Threshold, 'never'>, number> = { '7': 7, '30': 30, '90': 90 }

/**
 * How long after arming a confirm click is ignored.
 *
 * Longer than a platform double-click interval (500ms on macOS at the slowest
 * setting), so the second half of a double-click on the arm button can never be
 * received as consent to delete.
 */
const CONFIRM_ARM_MS = 600

/** Bytes as GB/MB. Sizes here span kilobytes to tens of gigabytes, so a single
 *  unit either loses the small rows or makes the big ones unreadable. */

/** Server bucket labels are stable ids; translate them for display. */
function bucketLabel(label: string): string {
  switch (label) {
    case 'under_7d': return i18nT('pages.sessionStorage.bucket_under_7d')
    case '7_30d': return i18nT('pages.sessionStorage.bucket_7_30d')
    case '30_90d': return i18nT('pages.sessionStorage.bucket_30_90d')
    case 'over_90d': return i18nT('pages.sessionStorage.bucket_over_90d')
    default: return label
  }
}

function bucketHint(label: string): string {
  switch (label) {
    case 'under_7d': return i18nT('pages.sessionStorage.bucket_under_7d_hint')
    case '7_30d': return i18nT('pages.sessionStorage.bucket_7_30d_hint')
    case '30_90d': return i18nT('pages.sessionStorage.bucket_30_90d_hint')
    case 'over_90d': return i18nT('pages.sessionStorage.bucket_over_90d_hint')
    default: return ''
  }
}

/** Bytes a given threshold would reclaim: every bucket at or beyond it. */
function reclaimableAt(buckets: SessionStorageBucket[], days: number): number {
  const from = days <= 7 ? 1 : days <= 30 ? 2 : 3
  return buckets.slice(from).reduce((sum, b) => sum + b.bytes, 0)
}

function thresholdSegments(): Segment<Threshold>[] {
  return [
    { key: '7', label: i18nT('pages.sessionStorage.threshold_7d') },
    { key: '30', label: i18nT('pages.sessionStorage.threshold_30d') },
    { key: '90', label: i18nT('pages.sessionStorage.threshold_90d') },
    { key: 'never', label: i18nT('pages.sessionStorage.threshold_never') },
  ]
}

export default function SessionStorageScreen({ onBack }: { onBack: () => void }) {
  const qc = useQueryClient()
  const [threshold, setThreshold] = useState<Threshold>('30')
  // Emptying is the one irreversible step, so it is two deliberate actions:
  // the button arms, a confirm commits. Armed state is per batch, and the
  // timestamp is what lets the confirm refuse a click that is really the second
  // half of a double-click on the arm button.
  const [arming, setArming] = useState<string | null>(null)
  const [armedAt, setArmedAt] = useState(0)
  const arm = (id: string | null) => {
    setArming(id)
    setArmedAt(id === null ? 0 : Date.now())
  }

  // Uncached on purpose: this walks the stores, and a stale figure would be
  // offered as the basis for a delete.
  const { data, isLoading } = useQuery<SessionStorageReport>({
    queryKey: ['session-storage'],
    queryFn: api.sessionStorage,
    refetchOnWindowFocus: false,
  })

  const invalidate = () => { void qc.invalidateQueries({ queryKey: ['session-storage'] }) }

  const cleanup = useMutation({
    mutationFn: (days: number) => api.sessionStorageCleanup(days),
    onSuccess: invalidate,
  })
  const restore = useMutation({
    mutationFn: (batchId: string) => api.sessionStorageRestore(batchId),
    onSuccess: invalidate,
  })
  const empty = useMutation({
    mutationFn: (batchId: string) => api.sessionStorageEmpty([batchId]),
    onSuccess: () => { setArming(null); invalidate() },
  })

  const busy = cleanup.isPending || restore.isPending || empty.isPending

  return (
    <div className="flex flex-col gap-4">
      <button
        type="button"
        onClick={onBack}
        className="self-start flex items-center gap-1 text-[11.5px] text-muted hover:text-text transition-colors"
      >
        <ChevronLeft className="w-3.5 h-3.5" />
        <span>{i18nT('pages.sessionStorage.back_to_disk')}</span>
      </button>

      {isLoading || !data ? (
        <Card><ContentSkeleton rows={6} /></Card>
      ) : (
        <Card>
          <Headline data={data} />
          <BucketTable buckets={data.buckets} />
          <ThresholdRow
            data={data}
            threshold={threshold}
            onThreshold={setThreshold}
            busy={busy}
            onStage={days => cleanup.mutate(days)}
          />
          <TrashSection
            data={data}
            busy={busy}
            arming={arming}
            armedAt={armedAt}
            onArm={arm}
            onRestore={id => restore.mutate(id)}
            onEmpty={id => empty.mutate(id)}
          />
          <FactStrip data={data} />
        </Card>
      )}
    </div>
  )
}

function Headline({ data }: { data: SessionStorageReport }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <span className="text-2xl font-semibold text-accent font-mono tabular-nums">
        {fmtBytes(data.reclaimable_bytes)}
      </span>
      <span className="text-[11.5px] text-muted">
        {i18nT('pages.sessionStorage.headline_detail', {
          reclaimable: fmtNumber(data.reclaimable_sessions),
          active: fmtBytes(data.active_bytes),
        })}
      </span>
    </div>
  )
}

function BucketTable({ buckets }: { buckets: SessionStorageBucket[] }) {
  const max = Math.max(1, ...buckets.map(b => b.bytes))
  return (
    <div className="mt-4 border-t border-border">
      <div
        className="py-2 text-[10.5px] uppercase tracking-wide text-muted border-b border-border"
        style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 96px 104px 22%', gap: '0.75rem' }}
      >
        <span>{i18nT('pages.sessionStorage.col_last_active')}</span>
        <span className="text-right">{i18nT('pages.sessionStorage.col_sessions')}</span>
        <span className="text-right">{i18nT('pages.sessionStorage.col_size')}</span>
        <span />
      </div>
      {buckets.map(b => (
        <div
          key={b.label}
          className="py-2.5 border-b border-border last:border-0 items-center"
          style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 96px 104px 22%', gap: '0.75rem' }}
        >
          <div className="min-w-0">
            <div className="text-[12.5px] text-text-strong">{bucketLabel(b.label)}</div>
            <div className="text-[11px] text-muted mt-0.5">{bucketHint(b.label)}</div>
          </div>
          <div className="text-right text-[12px] text-muted font-mono tabular-nums">{fmtNumber(b.sessions)}</div>
          <div className="text-right text-[12px] text-text font-mono tabular-nums">{fmtBytes(b.bytes)}</div>
          <div className="h-1 rounded-full bg-bg-elevated overflow-hidden">
            <div className="h-full bg-border-strong" style={{ width: `${(b.bytes / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

function ThresholdRow({
  data, threshold, onThreshold, busy, onStage,
}: {
  data: SessionStorageReport
  threshold: Threshold
  onThreshold: (t: Threshold) => void
  busy: boolean
  onStage: (days: number) => void
}) {
  const blocked = data.reclaim_blocked_reason !== ''
  const days = threshold === 'never' ? null : THRESHOLD_DAYS[threshold]
  const wouldReclaim = days === null ? 0 : reclaimableAt(data.buckets, days)

  return (
    <>
      <div className="mt-4 rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-wrap items-center gap-3">
        <span className="text-[11.5px] text-muted">{i18nT('pages.sessionStorage.retire_older_than')}</span>
        <SegmentedControl<Threshold>
          segments={thresholdSegments()}
          value={threshold}
          onChange={onThreshold}
          layoutId="storage-threshold"
          collapse={false}
        />
        <span className="text-[11px] text-muted font-mono tabular-nums">
          {data.buckets.length > 0 && [7, 30, 90].map(d => (
            <span key={d} className="mr-2">
              {i18nT('pages.sessionStorage.preview_days', { days: d })} {fmtBytes(reclaimableAt(data.buckets, d))}
            </span>
          ))}
        </span>
        <div className="ml-auto">
          <Btn
            primary
            disabled={blocked || busy || days === null || wouldReclaim === 0}
            onClick={() => { if (days !== null) onStage(days) }}
          >
            {days === null
              ? i18nT('pages.sessionStorage.nothing_to_move')
              : i18nT('pages.sessionStorage.move_to_trash', { size: fmtBytes(wouldReclaim) })}
          </Btn>
        </div>
      </div>

      {blocked ? (
        <div className="mt-2 flex items-start gap-2 text-[11.5px] text-warn">
          <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <span>{data.reclaim_blocked_reason}</span>
        </div>
      ) : (
        <p className="mt-2 text-[11.5px] text-muted leading-relaxed">
          {data.trash.instant
            ? i18nT('pages.sessionStorage.note_instant')
            : i18nT('pages.sessionStorage.note_copy')}{' '}
          {i18nT('pages.sessionStorage.note_active_excluded')}{' '}
          <strong className="text-danger">{i18nT('pages.sessionStorage.note_only_irreversible')}</strong>
        </p>
      )}
    </>
  )
}

function TrashSection({
  data, busy, arming, armedAt, onArm, onRestore, onEmpty,
}: {
  data: SessionStorageReport
  busy: boolean
  arming: string | null
  armedAt: number
  onArm: (id: string | null) => void
  onRestore: (id: string) => void
  onEmpty: (id: string) => void
}) {
  /** A confirm that arrives within the double-click window is not consent. */
  const onConfirm = (id: string) => {
    if (Date.now() - armedAt < CONFIRM_ARM_MS) return
    onEmpty(id)
  }
  const batches = data.trash.batches
  if (batches.length === 0) {
    return (
      <div className="mt-4 rounded-md border border-dashed border-border px-3 py-2.5 flex items-center justify-between">
        <div>
          <div className="text-[12.5px] text-text-strong">{i18nT('pages.sessionStorage.trash')}</div>
          <div className="text-[11px] text-muted mt-0.5">{i18nT('pages.sessionStorage.trash_never_expires')}</div>
        </div>
        <span className="text-[11.5px] text-muted font-mono">{i18nT('pages.sessionStorage.trash_empty')}</span>
      </div>
    )
  }

  return (
    <div className="mt-4">
      <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5">
        <div className="text-[12.5px] text-text-strong">
          {i18nT('pages.sessionStorage.trash_holds', { size: fmtBytes(data.trash.bytes) })}
        </div>
        <div className="text-[11px] text-muted mt-0.5">{i18nT('pages.sessionStorage.trash_still_on_disk')}</div>
      </div>

      {batches.map(b => (
        <div
          key={b.batch_id}
          className="mt-2 rounded-md border border-border px-3 py-2.5 flex flex-wrap items-center gap-3"
        >
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] text-text-strong font-mono">{b.batch_id}</div>
            <div className="text-[11px] text-muted mt-0.5">
              {i18nT('pages.sessionStorage.batch_detail', {
                sessions: fmtNumber(b.sessions),
                size: fmtBytes(b.bytes),
                reason: b.reason,
              })}
            </div>
          </div>
          <Btn disabled={busy} onClick={() => onRestore(b.batch_id)}>
            <Undo2 className="w-3.5 h-3.5" />
            <span>{i18nT('pages.sessionStorage.restore')}</span>
          </Btn>
          {arming === b.batch_id ? (
            /* Cancel FIRST, so it — not the destructive confirm — occupies the slot
               the arm button just vacated. A fast double-click on "Delete forever"
               would otherwise land its second click on a confirm that appeared
               under the stationary pointer. The time guard below covers the same
               hazard for a keyboard repeat or a re-ordered layout. */
            <>
              <Btn disabled={busy} onClick={() => onArm(null)}>
                {i18nT('pages.sessionStorage.cancel')}
              </Btn>
              <Btn danger disabled={busy} onClick={() => onConfirm(b.batch_id)}>
                {i18nT('pages.sessionStorage.confirm_delete_forever')}
              </Btn>
            </>
          ) : (
            <Btn disabled={busy} onClick={() => onArm(b.batch_id)}>
              <Trash2 className="w-3.5 h-3.5" />
              <span>{i18nT('pages.sessionStorage.delete_forever')}</span>
            </Btn>
          )}
        </div>
      ))}
    </div>
  )
}

function FactStrip({ data }: { data: SessionStorageReport }) {
  return (
    <div className="border-t border-border pt-3 mt-4 flex flex-wrap gap-x-6 gap-y-1 text-[11.5px] text-muted">
      <span>
        {i18nT('pages.sessionStorage.fact_floor')}:{' '}
        <strong className="text-text font-mono">{i18nT('pages.sessionStorage.fact_floor_value')}</strong>
      </span>
      <span>
        {i18nT('pages.sessionStorage.fact_instant')}:{' '}
        <strong className="text-text font-mono">
          {data.trash.instant
            ? i18nT('pages.sessionStorage.fact_instant_rename')
            : i18nT('pages.sessionStorage.fact_instant_copy')}
        </strong>
      </span>
      <span>
        {i18nT('pages.sessionStorage.fact_active')}:{' '}
        <strong className="text-text font-mono">{i18nT('pages.sessionStorage.fact_active_value')}</strong>
      </span>
      <span>
        {i18nT('pages.sessionStorage.fact_total')}:{' '}
        <strong className="text-text font-mono tabular-nums">
          {fmtBytes(data.total_bytes)} · {fmtNumber(data.total_sessions)}
        </strong>
      </span>
    </div>
  )
}
