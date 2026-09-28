import { memo } from 'react'
import { useQuery } from '@tanstack/react-query'

import {
  Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle,
} from '../../components/ui/dialog'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { api } from '../../api/client'
import { memoryQueryRetry } from '../overview/MemoryStoreCard'
import type { MemoryRecord } from '../../types/memoryEditing'

interface MemoryPopoverProps {
  /** The recalled-memory id to look up, or `null` when the dialog is closed. */
  recordId: string | null
  /**
   * The store the id belongs to, threaded from the recall record. A `memory.recall`
   * runs against the caller's own store — global (`'default'`) or a member's (V2) —
   * so the lookup has to use the SAME store, or a member recall's ids would all read
   * as "not found". Absent on the wire, the record's reader defaults it to `'default'`.
   */
  store: string
  onClose: () => void
}

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : error ? String(error) : ''

/**
 * The full text of one recalled memory, opened from an id on the recall strip.
 *
 * The ids the strip prints are episode ids, and an episode's `key` is null, so a
 * free-text `api.memoryRecords({ q })` can never find one — it would always miss,
 * or fuzzy-match a DIFFERENT record and show the wrong text. This resolves the id
 * through `api.memoryRecordsRefresh`, whose lookup is by `{ kind, id }` identity,
 * and still renders a record only on an EXACT id match: `refresh` returns
 * `{ entries, missing }`, and a near-miss is treated as "not found" rather than
 * shown as this memory.
 *
 * The lookup runs in the `store` the recall came from, not always the default one,
 * so a member (V2) recall's ids resolve in their own store.
 *
 * The query is keyed by `store` and `recordId`, so clicking a second id while the
 * first is in flight switches keys — React Query drops the stale result instead of
 * letting an earlier `.then()` paint the wrong record over the newer one.
 */
const MemoryPopover = memo(function MemoryPopover({ recordId, store, onClose }: MemoryPopoverProps) {
  // memo() bails out of the provider-level repaint; subscribe so a language switch
  // repaints this dialog's strings.
  useLanguageGeneration()
  const query = useQuery({
    queryKey: ['memory-record-detail', store, recordId],
    queryFn: () => api.memoryRecordsRefresh(store, [{ kind: 'episode', id: recordId! }]),
    enabled: !!recordId,
    retry: memoryQueryRetry,
  })

  if (!recordId) return null

  // Only the record whose id is exactly the one asked for. `refresh` never fuzzy
  // matches, but a defensive identity check keeps the mismatch impossible.
  const record: MemoryRecord | undefined = query.data?.entries.find(row => row.id === recordId)
  const notFound = query.isSuccess && !record

  return (
    <Dialog open onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent maxWidth={480}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.chat.decisionStrip.memory_detail_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          {/* Size lives on this wrapper, not on the primitive: `DialogBody` owns its own
              typography (restyle ratchet), and the call site only gets to size its content. */}
          <div className="text-[13px]">
          {/* The id itself, in full: it is a store handle, so it is never clipped —
              there is nothing readable to lose by wrapping it instead. */}
          <div className="mb-3 font-mono text-[11px] text-muted [overflow-wrap:anywhere]">{recordId}</div>

          {query.isPending && (
            <p role="status" className="text-muted">
              {i18nT('pages.chat.decisionStrip.memory_detail_loading')}
            </p>
          )}

          {/* Hand-off ON: a failed read-only lookup holds no unsaved input to lose, and
              the id it could not resolve is a question the agent can act on. The dialog
              sits OVER the chat the hand-off navigates to, so `onHandoff` closes it
              first — a hand-off hidden behind the dialog reads as a dead button. The
              recall strip beneath stays mounted either way. */}
          <ErrorNotice
            message={query.isError ? errorText(query.error) : null}
            variant="inline"
            askAgent
            onHandoff={onClose}
          />

          {notFound && (
            <p role="status" className="text-muted">
              {i18nT('pages.chat.decisionStrip.memory_detail_not_found')}
            </p>
          )}

          {record && (
            <div className="flex flex-col gap-3">
              <div>
                <div className="mb-1 opacity-75">
                  {i18nT('pages.chat.decisionStrip.memory_detail_text_label')}
                </div>
                <div className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-bg p-2 font-mono text-[11px] text-muted">
                  {record.text || i18nT('pages.chat.decisionStrip.memory_detail_empty')}
                </div>
              </div>
            </div>
          )}
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
})

export default MemoryPopover
