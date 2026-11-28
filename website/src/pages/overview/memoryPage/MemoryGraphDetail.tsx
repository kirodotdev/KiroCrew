/**
 * One memory opened from the Graph tab, over the graph: the same detail view
 * the Memories tab shows (MemoryRecordDetail: a side panel on a wide screen, a
 * sheet on a phone), with the same edit, forget and history actions. The graph
 * stays mounted behind it, so closing returns to the same zoom and place.
 */
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import { api } from '../../../api/client'
import { IconButton } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtNumber } from '../../../i18n/format'
import type { MemoryRecordRef } from '../../../types/memoryEditing'
import { MEMORY_QUERY_PREFIXES, memoryQueryRetry } from '../MemoryStoreCard'
import { errorText } from '../memoryRecordParts'
import { useMemoryRecordEdit, type MemoryEditSelection } from '../useMemoryRecordEdit'
import { MemoryRecordDetail } from './MemoryRecordDetail'

/** The wide screen's panel width, as the Memories tab's (MemoryDetailSlide). */
const PANEL_PX = 360

export function MemoryGraphDetail({ store, record, nodeRef, phone, onClose, onRefresh }: {
  store: string
  /** The memory to open, and its `memory_record_facets.record_ref` to read it by. */
  record: MemoryRecordRef; nodeRef: string
  phone: boolean
  onClose: () => void
  onRefresh: () => void
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  // Under the Memories tab's own key prefix, so an edit anywhere refreshes it.
  const read = useQuery({
    queryKey: ['memory-records', store, 'graph-detail', nodeRef],
    queryFn: () => api.memoryRecords(store, { q: '', kind: 'all', filters: { ref: [nodeRef] } }, 0, 1),
    retry: memoryQueryRetry,
  })
  const row = read.data?.entries.find(r => r.kind === record.kind && r.id === record.id) ?? read.data?.entries[0] ?? null
  const [feedback, setFeedback] = useState('')
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['memory-records', store] })
    void client.invalidateQueries({ queryKey: ['member-memory', store] })
    void client.invalidateQueries({ queryKey: ['memory-record-history', store] })
    onRefresh()
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
  }
  // The one memory open here is the whole selection an edit acts on.
  const selection: MemoryEditSelection = {
    count: row ? 1 : 0,
    current: () => ({ items: row?.revision ? [{ kind: row.kind, id: row.id, revision: row.revision }] : [] }),
    onMatchedCount: () => {}, onItemsRefreshed: () => {}, clear: () => {},
  }
  const edit = useMemoryRecordEdit({
    store, refresh, selection,
    onApplied: changed => { setFeedback(t('memoryEditing.applied', { count: fmtNumber(changed) })); refresh() },
  })
  // Gone from the store (forgotten here or elsewhere): nothing is left to show.
  const gone = !!read.data && !row && !read.error
  useEffect(() => { if (gone) onClose() }, [gone, onClose])
  const closeButton = <IconButton aria-label={t('components.modal.close')} className="flex h-8 w-8 shrink-0 items-center justify-center" onClick={onClose}><X className="lucide-inline" aria-hidden="true" /></IconButton>
  let content
  if (row) {
    content = <MemoryRecordDetail store={store} row={row} phone={phone} fadeLine={read.data?.decay?.fade_line} onClose={onClose}
      onEdit={() => edit.begin({ type: 'set', row })} onForget={() => edit.begin({ type: 'forget', row })}
      onRestore={revisionId => edit.restore(row, revisionId)} onKeepCurrent={() => edit.keepCurrent(row)}
      onProposal={(proposal, operation) => edit.takeProposal(row, proposal, operation)} />
  } else if (read.error) {
    content = <div className="flex items-start gap-2 rounded-2xl border border-border bg-card p-4">
      <div className="min-w-0 flex-1"><ErrorNotice askAgent title={t('memoryPage.read_failed', { store: store || t('pages.kiroCrewAgentsPage.global_memory_v1') })} message={errorText(read.error)} /></div>
      {closeButton}
    </div>
  }
  const notice = feedback && <div role="status" className="mb-2 flex items-center gap-2 rounded-lg bg-ok-subtle px-3 py-2 text-[13px] text-ok-text">{feedback}<IconButton aria-label={t('components.errorNotice.dismiss')} className="ml-auto flex h-8 w-8 items-center justify-center" onClick={() => setFeedback('')}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>
  return <>
    {/* A phone's detail is its own sheet (MemoryRecordDetail opens a Modal); a wide
        screen's sits over the graph's right edge, scrolling on its own. */}
    {phone ? <>{notice && <div className="absolute inset-x-3 top-3 z-10">{notice}</div>}{content}</> : (content || notice) && <div data-testid="memory-graph-detail" className="absolute bottom-3 right-3 top-3 z-10 overflow-y-auto overscroll-contain" style={{ width: PANEL_PX, maxWidth: 'calc(100% - 24px)' }}>{notice}{content}</div>}
    {edit.dialog}
  </>
}
