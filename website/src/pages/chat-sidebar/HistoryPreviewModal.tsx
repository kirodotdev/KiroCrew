/** Read-only preview of a closed session, opened from the Older Sessions pane.
 *
 *  Resuming clears the transcript's `closed` flag and publishes a live tab, so
 *  it cannot double as a way to look at an old conversation. This dialog reads
 *  the transcript through `GET /api/sessions/{key}`, which changes
 *  nothing, and leaves reopening to its own explicit Resume button. */
import { useQuery } from '@tanstack/react-query'
import { History } from 'lucide-react'

import { api } from '../../api/client'
import { ChatTranscriptSkeleton } from '../../components/ChatTranscriptSkeleton'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { parseErrorCode, reportForError } from '../../utils/errorReport'
import Modal from '../../components/Modal'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'

/** Roles the preview draws. Tool rows, system notices and in-flight state are
 *  the live tab's business; a reader skimming an old conversation wants the
 *  exchange itself. */
const SHOWN_ROLES = new Set(['user', 'assistant', 'streaming'])

/** Plain wording for the failures the preview read can name. Anything else
 *  keeps the request's own message, which is also the key `ErrorNotice` uses to
 *  find the structured report behind "Ask the agent". */
function previewFailureMessage(error: unknown): string {
  const body = (error as { body?: unknown } | null)?.body
  const code = parseErrorCode(typeof body === 'string' ? body : undefined)
  if (code === 'transcript_changed') return i18nT('pages.chatSidebar.history_preview_changed')
  if (code === 'session_not_found') return i18nT('pages.chatSidebar.history_preview_missing')
  if (code === 'history_corpus_unreadable' || code === 'no_conversation_log') return i18nT('pages.chatSidebar.history_preview_unreadable')
  return error instanceof Error ? error.message : String(error)
}

export interface HistoryPreviewTarget {
  key: string
  title: string
}

export default function HistoryPreviewModal({ target, onClose, onResume, resumeDisabled = false }: {
  /** The row being previewed; `null` keeps the dialog closed. */
  target: HistoryPreviewTarget | null
  onClose: () => void
  /** The explicit reopen. The caller resumes; this dialog only asks. */
  onResume: (target: HistoryPreviewTarget) => void
  /** Offline: the transcript may still be on screen, but a resume cannot run. */
  resumeDisabled?: boolean
}) {
  const key = target?.key ?? null
  // Keyed per session, so a late answer for a row the user has moved past is
  // cached under its own key and never painted over the current one. `gcTime: 0`
  // drops that entry when the dialog unmounts, which is what makes each open a
  // fresh read: the client default `staleTime: Infinity` would otherwise serve
  // the first page for a transcript that has since grown.
  const { data: preview, error, isPending, refetch, isFetching } = useQuery({
    queryKey: ['session-preview', key],
    queryFn: () => api.sessionDetail(key as string),
    enabled: key !== null,
    gcTime: 0,
  })

  const rows = preview
    ? preview.messages.filter(m => SHOWN_ROLES.has(m.role) && typeof m.content === 'string' && m.content.trim())
    : []

  // The sidebar row's title comes from `GET /api/sessions`, which does not
  // display-redact it; the preview read does. So only the loaded preview's
  // title may name the dialog. The localized generic label keeps the raw row
  // title out of the loading and failure states.
  const heading = preview?.title || i18nT('pages.chatSidebar.history_preview_transcript')

  return (
    <Modal
      open={target !== null}
      onClose={onClose}
      title={<span className="flex items-center gap-2 min-w-0"><History size={16} className="shrink-0" /><span className="truncate">{heading}</span></span>}
      ariaLabel={i18nT('pages.chatSidebar.history_preview_dialog', { title: heading })}
      maxWidth={760}
      height="75vh"
      footer={
        <div className="flex items-center justify-end gap-2">
          <Btn type="button" onClick={onClose}>{i18nT('pages.chatSidebar.close_preview')}</Btn>
          {/* After a failed read the dialog has shown nothing, so reopening the
              session must not be the emphasized way out of it: Resume drops to
              a plain button and Try again inside the notice is the next step. */}
          <Btn
            type="button"
            primary={!error}
            disabled={resumeDisabled || target === null}
            onClick={() => { if (target) onResume(target) }}
          >{i18nT('pages.chatSidebar.resume_session')}</Btn>
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-[12px] text-muted m-0">{i18nT('pages.chatSidebar.history_preview_notice')}</p>
        {isPending && !error && (
          // The transcript's own silhouette while it loads, so the dialog does
          // not sit blank; the status line carries the wait for screen readers.
          <div className="relative min-h-[260px]" aria-busy="true">
            <div role="status" className="sr-only">{i18nT('pages.chatSidebar.history_preview_loading')}</div>
            <ChatTranscriptSkeleton />
          </div>
        )}
        {error && (
          <ErrorNotice
            title={i18nT('pages.chatSidebar.history_preview_failed')}
            message={previewFailureMessage(error)}
            report={reportForError(error)}
            askAgent
            onHandoff={onClose}
            footer={
              <Btn
                type="button"
                primary
                className="px-2 py-0.5 text-[12px]"
                disabled={isFetching}
                onClick={() => { void refetch() }}
              >{i18nT('pages.chatSidebar.history_preview_retry')}</Btn>
            }
          />
        )}
        {preview?.has_more && (
          <p className="text-[12px] text-muted m-0">
            {i18nT('pages.chatSidebar.history_preview_truncated')}
          </p>
        )}
        {preview && rows.length === 0 && (
          <div className="text-[12px] text-muted">{i18nT('pages.chatSidebar.history_preview_empty')}</div>
        )}
        {rows.length > 0 && (
          <ol className="list-none m-0 p-0 flex flex-col gap-3" aria-label={i18nT('pages.chatSidebar.history_preview_transcript')}>
            {rows.map((m, i) => (
              <li key={i} className={m.role === 'user' ? 'self-end max-w-[85%]' : 'max-w-full'}>
                <div className="text-[11px] font-semibold text-muted mb-1">
                  {m.role === 'user' ? i18nT('pages.chatSidebar.history_preview_you') : i18nT('pages.chatSidebar.history_preview_agent')}
                </div>
                {m.role === 'user'
                  ? <div className="rounded-lg bg-bg-elevated border border-border px-3 py-2 whitespace-pre-wrap break-words">{m.content}</div>
                  : <MarkdownRenderer content={m.content} readOnlyCode blockedLinks={m.meta?.blocked_links} redactions={m.meta?.redactions} />}
              </li>
            ))}
          </ol>
        )}
      </div>
    </Modal>
  )
}
