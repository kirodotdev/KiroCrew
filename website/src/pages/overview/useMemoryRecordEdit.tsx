/**
 * Edit, forget, find-and-replace and restore for memory records: the preview,
 * its pages, the apply, the conflict refresh and the dialog that holds them.
 * Shared by `MemoryRecordsEditor` (the bot editor dialog) and the memory page.
 */
import { useCallback, useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronLeft, ChevronRight, ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'
import { Badge, Btn, Input, PanelSectionHeader } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { fmtDateTimeNumeric, fmtNumber } from '../../i18n/format'
import type { MemoryEditOperation, MemoryEditPreview, MemoryRecord, MemoryRecordQuery, MemoryRecordSelection } from '../../types/memoryEditing'
import { MEMORY_RECORD_LABELS, RecordProvenance, errorText, memoryRecordBody, recordIdentity, recordReplacement, shortBody } from './memoryRecordParts'

export type MemoryEditIntent = { type: 'set'; row: MemoryRecord } | { type: 'replace_text' } | { type: 'forget'; row?: MemoryRecord } | { type: 'restore'; row: MemoryRecord; revisionId: number }

/** The caller's multi-record selection, which a preview without a named row reaches. */
export type MemoryEditSelection = {
  count: number
  current: () => MemoryRecordSelection
  /** A query selection's exact count, from a preview or a refresh. */
  onMatchedCount: (count: number) => void
  /** The selected rows as they are now, after a refresh. */
  onItemsRefreshed: (entries: MemoryRecord[]) => void
  clear: () => void
}

/** Hand-picked records past this need "select all matching" instead. */
export const MANUAL_LIMIT = 500
type QuerySelection = { query: MemoryRecordQuery; count: number; exclude: Map<string, MemoryRecord> }

/** A list's multi-record selection: rows picked by hand, or every record a query
 *  matches minus the rows unticked since. */
export function useMemoryRecordSelection() {
  const [selected, setSelected] = useState<Map<string, MemoryRecord>>(new Map())
  const [all, setAll] = useState<QuerySelection | null>(null)
  const count = all ? all.count : selected.size
  // Stable while the picks stay the same, so a memoized row list can skip re-rendering.
  const isSelected = useCallback((row: MemoryRecord) => all ? !all.exclude.has(recordIdentity(row)) : selected.has(recordIdentity(row)), [all, selected])
  // Stable, so a list can clear its picks from an effect keyed on what it lists.
  const clear = useCallback(() => { setSelected(new Map()); setAll(null) }, [])
  const toggle = (row: MemoryRecord) => {
    const id = recordIdentity(row)
    if (all) { setAll(old => { const exclude = new Map(old!.exclude); if (exclude.has(id)) exclude.delete(id); else if (exclude.size < MANUAL_LIMIT) exclude.set(id, row); return { ...old!, count: Math.max(0, old!.count + old!.exclude.size - exclude.size), exclude } }); return }
    setSelected(old => { const next = new Map(old); if (next.has(id)) next.delete(id); else if (next.size < MANUAL_LIMIT) next.set(id, row); return next })
  }
  const choosePage = (rows: MemoryRecord[]) => {
    if (all) { setAll(old => { const exclude = new Map(old!.exclude); const every = rows.every(row => !exclude.has(recordIdentity(row))); for (const row of rows) { if (every) { if (exclude.size < MANUAL_LIMIT) exclude.set(recordIdentity(row), row) } else exclude.delete(recordIdentity(row)) } return { ...old!, count: Math.max(0, old!.count + old!.exclude.size - exclude.size), exclude } }); return }
    setSelected(old => { const next = new Map(old); const every = rows.every(row => next.has(recordIdentity(row))); for (const row of rows) { if (every) next.delete(recordIdentity(row)); else if (next.size < MANUAL_LIMIT) next.set(recordIdentity(row), row) } return next })
  }
  const selectAll = (query: MemoryRecordQuery, matched: number) => { setAll({ query, count: matched, exclude: new Map() }); setSelected(new Map()) }
  /** Whether a row's checkbox can change: a limit reached keeps it as it is. */
  const lockedRow = useCallback((row: MemoryRecord) => !row.revision || (!all && !isSelected(row) && count >= MANUAL_LIMIT) || (!!all && isSelected(row) && all.exclude.size >= MANUAL_LIMIT), [all, isSelected, count])
  const forEdit: MemoryEditSelection = {
    count,
    current: () => all ? { query: all.query, exclude: [...all.exclude.values()].map(({ kind, id }) => ({ kind, id })) }
      : { items: [...selected.values()].map(({ kind, id, revision }) => ({ kind, id, revision })) },
    onMatchedCount: matched => setAll(old => old ? { ...old, count: matched } : old),
    onItemsRefreshed: entries => setSelected(new Map(entries.map(row => [recordIdentity(row), row]))),
    clear,
  }
  return { all, count, isSelected, clear, toggle, choosePage, selectAll, lockedRow, forEdit }
}

export function useMemoryRecordEdit({ store, selection, refresh, onApplied, onBegin }: {
  store: string
  selection: MemoryEditSelection
  refresh: () => void
  onApplied: (changedCount: number) => void
  onBegin?: () => void
}) {
  const { t } = useTranslation()
  const [intent, setIntent] = useState<MemoryEditIntent | null>(null)
  const [draft, setDraft] = useState('')
  const [find, setFind] = useState('')
  const [replaceWith, setReplaceWith] = useState('')
  const [matchCase, setMatchCase] = useState(false)
  const [selectionNotice, setSelectionNotice] = useState('')
  const [missingRecord, setMissingRecord] = useState(false)
  const [reviewedPreview, setReviewedPreview] = useState<MemoryEditPreview | null>(null)
  const [previewPageOffset, setPreviewPageOffset] = useState(0)
  // Retained across the retry: `previewPage.mutate` clears its own error the
  // moment it re-enters pending, so a Retry gated on `previewPage.error` alone
  // would unmount -- and drop its keyboard focus -- for the whole retry. Set on
  // a failed page read, cleared on success (below) or when the mutation is
  // reset back to idle anywhere (adjust, apply, a new intent): the effect
  // covers every `previewPage.reset()` call site at once.
  const [previewPageFailed, setPreviewPageFailed] = useState(false)
  const namedRow = intent && 'row' in intent ? intent.row : undefined
  const current = (): MemoryRecordSelection => namedRow ? { items: [{ kind: namedRow.kind, id: namedRow.id, revision: namedRow.revision }] } : selection.current()
  const previewPage = useMutation({ mutationFn: ({ previewId, offset }: { previewId: string; offset: number }) => api.memoryEditPreviewPage(store, previewId, offset), onSuccess: result => { setPreviewPageFailed(false); setReviewedPreview(result) }, onError: () => setPreviewPageFailed(true) })
  const preview = useMutation({ mutationFn: ({ row, operation: chosen }: { row?: MemoryRecord; operation?: MemoryEditOperation }) => {
    let operation: MemoryEditOperation
    if (row) return api.memoryEditPreview(store, { items: [{ kind: row.kind, id: row.id, revision: row.revision }] }, chosen || recordReplacement(row, memoryRecordBody(row)))
    if (intent?.type === 'set') {
      try { operation = recordReplacement(intent.row, draft) } catch { throw new Error(t('memoryV2.structured_error')) }
    } else if (intent?.type === 'restore') operation = { type: 'restore', revision_id: intent.revisionId }
    else operation = intent?.type === 'forget' ? { type: 'forget' } : { type: 'replace_text', find, replacement: replaceWith, match_case: matchCase }
    return api.memoryEditPreview(store, current(), operation)
  }, onSuccess: result => {
    previewPage.reset(); setPreviewPageOffset(0); setReviewedPreview(result)
    if (!namedRow) selection.onMatchedCount(result.matched_count)
  } })
  const apply = useMutation({ mutationFn: () => api.memoryEditApply(store, preview.data!.preview_id), onSuccess: result => {
    setIntent(null); selection.clear(); preview.reset(); previewPage.reset(); setReviewedPreview(null); onApplied(result.changed_count)
  } })
  const refreshSelection = useMutation({ mutationFn: async () => {
    const chosen = current()
    if ('query' in chosen) return api.memoryQuerySelectionRefresh(store, chosen)
    return api.memoryRecordsRefresh(store, chosen.items.map(({ kind, id }) => ({ kind, id })))
  }, onSuccess: result => {
    if ('matched_count' in result) {
      selection.onMatchedCount(result.matched_count)
    } else if (namedRow && intent) {
      const fresh = result.entries.find(row => recordIdentity(row) === recordIdentity(namedRow))
      setMissingRecord(!fresh)
      if (fresh) setIntent({ ...intent, row: fresh } as MemoryEditIntent)
    } else selection.onItemsRefreshed(result.entries)
    preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset(); refresh()
    setSelectionNotice(t('missing' in result && result.missing.length ? 'memoryEditing.missing_selection' : 'memoryEditing.selection_refreshed'))
  } })
  const applyConflict = !!apply.error && typeof apply.error === 'object' && 'status' in apply.error && (apply.error.status === 409 || apply.error.status === 400)
  const previewPageConflict = !!previewPage.error && typeof previewPage.error === 'object' && 'status' in previewPage.error && previewPage.error.status === 409
  const busy = preview.isPending || previewPage.isPending || apply.isPending || refreshSelection.isPending
  // A `previewPage.reset()` anywhere (adjust, apply, a new intent) returns the
  // mutation to idle; drop the retained failed flag with it so the Retry does
  // not linger past a reset.
  useEffect(() => { if (previewPage.isIdle) setPreviewPageFailed(false) }, [previewPage.isIdle])
  const begin = (next: MemoryEditIntent) => { preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset(); refreshSelection.reset(); setSelectionNotice(''); setMissingRecord(false); setIntent(next); onBegin?.(); if (next.type === 'set') setDraft(memoryRecordBody(next.row)) }
  /** Open a restore of one revision and read its preview at once. */
  const restore = (row: MemoryRecord, revisionId: number) => { begin({ type: 'restore', row, revisionId }); preview.mutate({ row, operation: { type: 'restore', revision_id: revisionId } }) }
  /** Resolve a pending proposal by writing the current value back. */
  const keepCurrent = (row: MemoryRecord) => { begin({ type: 'set', row }); preview.mutate({ row }) }
  /** Open an edit (or forget) with a proposal's value as the draft. */
  const takeProposal = (row: MemoryRecord, proposal: MemoryRecord, operation: 'set' | 'forget') => { begin(operation === 'forget' ? { type: 'forget', row } : { type: 'set', row }); if (operation === 'set') setDraft(memoryRecordBody(proposal)) }
  const intentCount = namedRow ? 1 : selection.count
  const title = intent?.type === 'set' ? t('memoryV2.edit_memory_title') : intent?.type === 'restore' ? t('memoryEditing.restore_title') : intent?.type === 'forget' ? namedRow ? t('memoryV2.forget') : t('memoryEditing.forget_count', { count: fmtNumber(intentCount) }) : t('memoryEditing.replace_title')

  const dialog = intent && <Modal open title={title} guardAccidentalDismiss onClose={busy ? () => {} : () => setIntent(null)} footer={<div className="flex flex-wrap justify-end gap-2">{(intent.type === 'forget' || intent.type === 'restore') && <Btn className="min-h-11" disabled={busy} onClick={() => setIntent(null)}>{t('components.confirmDialog.cancel')}</Btn>}{reviewedPreview && intent.type !== 'forget' && intent.type !== 'restore' && <Btn className="min-h-11" disabled={busy} onClick={() => { preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset() }}>{t('memoryEditing.adjust')}</Btn>}{reviewedPreview && !applyConflict ? <Btn primary={intent.type !== 'forget'} danger={intent.type === 'forget'} className="min-h-11" disabled={busy || !!previewPage.error || reviewedPreview.changed_count === 0} onClick={() => apply.mutate()}>{apply.isPending ? t('memoryEditing.applying') : intent.type === 'forget' ? t('memoryEditing.forget_count', { count: fmtNumber(reviewedPreview.changed_count) }) : intent.type === 'restore' && !apply.error ? t('memoryEditing.restore_version', { value: shortBody(reviewedPreview.entries[0]?.after ? memoryRecordBody(reviewedPreview.entries[0].after) : '') }) : apply.error ? t('memoryEditing.retry_apply') : t('memoryEditing.apply', { count: fmtNumber(reviewedPreview.changed_count) })}</Btn> : <Btn primary className="min-h-11" disabled={busy || missingRecord || (!namedRow && selection.count === 0) || (intent.type === 'set' && !draft.trim()) || (intent.type === 'replace_text' && !find)} onClick={() => { apply.reset(); refreshSelection.reset(); preview.mutate({}) }}>{preview.isPending ? t('memoryEditing.preparing') : t('memoryEditing.preview')}</Btn>}</div>}>
    <div className="space-y-4">
      {/* No hand-off: preserve the correction, selected records and reviewed preview. */}<ErrorNotice message={errorText(refreshSelection.error || preview.error || previewPage.error || apply.error)} />{selectionNotice && <p role="status" className="text-[13px] text-muted">{selectionNotice}</p>}{(applyConflict || preview.error || previewPageConflict || missingRecord) && <Btn className="min-h-11" disabled={busy} onClick={() => refreshSelection.mutate()}>{t('memoryEditing.refresh_selection')}</Btn>}{(previewPage.error || previewPageFailed) && !previewPageConflict && <Btn busy={busy} className="min-h-11" onClick={() => previewPage.mutate({ previewId: preview.data!.preview_id, offset: previewPageOffset })}>{t('memoryEditing.retry_preview_page')}</Btn>}
      {intent.type === 'forget' && <p className="text-[13px]">{t('memoryV2.forget_explanation')}</p>}
      {intent.type === 'restore' && <p className="text-[13px]">{t('memoryEditing.restore_hint')}</p>}
      {apply.error && <p className="text-[13px] text-muted">{t(applyConflict ? 'memoryEditing.conflict_hint' : 'memoryEditing.unconfirmed_hint')}</p>}
      {!reviewedPreview && <>{intent.type === 'set' && <><RecordProvenance row={intent.row} /><textarea aria-label={t('memoryV2.memory_text')} className="min-h-40 w-full rounded-lg border border-border bg-bg-elevated p-3 text-sm focus-ring" disabled={busy} value={draft} onChange={event => setDraft(event.target.value)} /></>}{intent.type === 'replace_text' && <><label className="block space-y-1 text-[13px]"><span>{t('memoryEditing.find')}</span><Input className="min-h-11 w-full" value={find} disabled={busy} onChange={event => setFind(event.target.value)} /></label><label className="block space-y-1 text-[13px]"><span>{t('memoryEditing.replacement')}</span><Input className="min-h-11 w-full" value={replaceWith} disabled={busy} onChange={event => setReplaceWith(event.target.value)} /></label><label className="flex min-h-11 items-center gap-2 text-[13px]"><input aria-label={t('memoryEditing.match_case')} type="checkbox" className="h-4 w-4 accent-accent" disabled={busy} checked={matchCase} onChange={event => setMatchCase(event.target.checked)} />{t('memoryEditing.match_case')}</label><p className="text-[12px] text-muted">{t('memoryEditing.replace_hint')}</p></>}{intent.type === 'forget' && intent.row && <p className="whitespace-pre-wrap break-words text-sm">{memoryRecordBody(intent.row)}</p>}</>}
      {reviewedPreview && <>{intent.type !== 'restore' && <div role="status" className="flex flex-wrap gap-2"><Badge variant="muted">{t('memoryEditing.will_change', { count: fmtNumber(reviewedPreview.changed_count) })}</Badge><Badge variant="muted">{t('memoryEditing.unchanged', { count: fmtNumber(reviewedPreview.unchanged_count) })}</Badge></div>}{intent.type !== 'restore' && <p className="text-[12px] text-muted">{t('memoryEditing.preview_expiry', { time: fmtDateTimeNumeric(reviewedPreview.expires_at) })}</p>}{reviewedPreview.changed_count > 0 && intent.type !== 'restore' && <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[12px] text-muted">{t('memoryEditing.page_range', { from: fmtNumber(reviewedPreview.preview_offset + 1), to: fmtNumber(reviewedPreview.preview_offset + reviewedPreview.entries.length), total: fmtNumber(reviewedPreview.changed_count) })}</span><div className="flex gap-2"><Btn aria-label={t('memoryEditing.previous')} className="min-h-11 min-w-11 justify-center" disabled={busy || !!previewPage.error || reviewedPreview.preview_offset === 0} onClick={() => { const offset = Math.max(0, reviewedPreview.preview_offset - reviewedPreview.preview_limit); setPreviewPageOffset(offset); previewPage.mutate({ previewId: preview.data!.preview_id, offset }) }}><ChevronLeft className="lucide-inline" /></Btn><Btn aria-label={t('memoryEditing.next')} className="min-h-11 min-w-11 justify-center" disabled={busy || !!previewPage.error || !reviewedPreview.preview_has_more} onClick={() => { const offset = reviewedPreview.preview_offset + reviewedPreview.preview_limit; setPreviewPageOffset(offset); previewPage.mutate({ previewId: preview.data!.preview_id, offset }) }}><ChevronRight className="lucide-inline" /></Btn></div></div>}<div className="max-h-96 space-y-3 overflow-y-auto">{reviewedPreview.entries.map(({ before, after, operation }) => <div key={recordIdentity(before)} className="rounded-xl border border-border p-3">{operation === 'resolve' && <p className="mb-3 flex items-start gap-2 text-[13px] text-accent"><ShieldCheck className="lucide-inline mt-0.5 shrink-0" />{t('memoryEditing.resolve_explanation')}</p>}<Badge variant="muted">{t(MEMORY_RECORD_LABELS[before.kind])}</Badge><div className="mt-2 grid min-w-0 gap-3 sm:grid-cols-2"><div className="min-w-0"><PanelSectionHeader label={t(intent.type === 'restore' ? 'memoryEditing.restore_now' : 'memoryEditing.before')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{memoryRecordBody(before)}</p></div><div className="min-w-0"><PanelSectionHeader label={t(intent.type === 'restore' ? 'memoryEditing.restore_after' : 'memoryEditing.after')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{after ? memoryRecordBody(after) : t('memoryEditing.removed')}</p></div></div></div>)}</div>{reviewedPreview.warnings.map((warning, index) => <p key={index} className="text-[13px] text-muted">{warning}</p>)}</>}
    </div>
  </Modal>
  return { intent, begin, restore, keepCurrent, takeProposal, busy, dialog }
}
