/**
 * The one status line a Quick edit leaves on the artifact page: working while the
 * agent rewrites, saved once the live version rises, or finished without a new
 * version once its turn has started and ended. Read from state the page already
 * has (the `['artifact', slug]` record the `artifact_update` event refreshes, and
 * the session's turn count and busy reads), so it needs no polling and no timer.
 * In memory only: another artifact, a new Quick edit or leaving the page clears it.
 */
import { useCallback, useState } from 'react'
import { Loader2, X } from 'lucide-react'
import { useAppSelector } from '../store'
import { selectSlotRunEpoch } from '../store/chatSlice'
import { slotBusy } from '../components/chat-input/busySend'
import { i18nT } from '../i18n/t'

/** A Quick edit the server took, with where things stood at the send; or one refused
 *  because the session was busy. */
export type QuickEditSent =
  | { slug: string; action: string; whole: boolean; slot: string; epoch: number; version: number }
  | { slug: string; busy: true }

export function useQuickEditStatus(slug: string, liveVersion: number | undefined) {
  const [sent, setSent] = useState<QuickEditSent | null>(null)
  const shown = sent?.slug === slug ? sent : null
  const edit = shown && !('busy' in shown) ? shown : null
  const epoch = useAppSelector((s) => (edit ? selectSlotRunEpoch(s, edit.slot) : 0))
  const busy = useAppSelector((s) => (edit ? slotBusy(s, edit.slot) : false))
  const clear = useCallback(() => setSent(null), [])
  let line: string | null = null
  let working = false
  if (shown && !edit) {
    line = i18nT('pages.artifactDetailPage.quick_edit_busy')
  } else if (edit && liveVersion !== undefined && liveVersion > edit.version) {
    line = i18nT('pages.artifactDetailPage.quick_edit_saved', { action: edit.action, version: liveVersion })
  } else if (edit && epoch > edit.epoch && !busy) {
    line = i18nT('pages.artifactDetailPage.quick_edit_unchanged', { action: edit.action })
  } else if (edit) {
    working = true
    line = i18nT(edit.whole
      ? 'pages.artifactDetailPage.quick_edit_working_document'
      : 'pages.artifactDetailPage.quick_edit_working_selection', { action: edit.action })
  }
  return { line, working, track: setSent, clear }
}

export type QuickEditStatus = ReturnType<typeof useQuickEditStatus>

export function QuickEditStatusLine({ status }: { status: QuickEditStatus }) {
  if (!status.line) return null
  return (
    <div
      role="status"
      aria-label={i18nT('pages.artifactDetailPage.quick_edits')}
      className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-accent/30 bg-accent-subtle text-[13px] text-text"
    >
      {status.working && <Loader2 size={14} className="lucide-inline shrink-0 mt-0.5 animate-spin" aria-hidden="true" />}
      <span className="min-w-0 flex-1">{status.line}</span>
      {!status.working && (
        <button
          type="button"
          className="shrink-0 mt-[2px] bg-transparent border-none p-0 cursor-pointer text-muted hover:text-text-strong transition-colors"
          aria-label={i18nT('components.errorNotice.dismiss')}
          onClick={status.clear}
        >
          <X size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
