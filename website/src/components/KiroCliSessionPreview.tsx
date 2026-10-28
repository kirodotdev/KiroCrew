import { useId, useState } from 'react'
import { useInfiniteQuery, useMutation } from '@tanstack/react-query'
import { FolderOpen, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import type { KiroCliContinueResult, KiroCliSessionPage } from '../api/client/sessions'
import { useAppDispatch } from '../store'
import { addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import { requestSlotFolderOpen, switchSlot } from '../store/chatSlice'
import { errMessage } from '../utils/thunkError'
import { fmtRelativeTime } from '../pages/chat/sessionOrder'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { i18nT } from '../i18n/t'

/** Messages per page of the preview; the gateway caps a page at 500. */
const PAGE = 100

/**
 * A read-only look at one kiro-cli session a person ran in a terminal, with the
 * one action it offers: Continue in Crew.
 *
 * The gateway serves the conversation newest page first; "Show earlier messages"
 * walks back a page at a time. Nothing here can change the terminal session:
 * Continue COPIES it into a new chat (`POST /api/kiro-cli/sessions/{id}/continue`)
 * and that chat resumes with the session's full context.
 *
 * On success the new chat opens and the dialog closes. When the session's
 * folder could not be used as the chat's project (it was deleted, or overlaps
 * Crew's own data), the dialog stays open long enough to say so, because the
 * chat would otherwise open in a different folder with no sign of why.
 */
export default function KiroCliSessionPreview({ sessionId, onClose }: { sessionId: string | null; onClose: () => void }) {
  return (
    <Dialog open={sessionId !== null} onOpenChange={open => { if (!open) onClose() }}>
      {sessionId !== null && <PreviewContent key={sessionId} sessionId={sessionId} onClose={onClose} />}
    </Dialog>
  )
}

function PreviewContent({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const dispatch = useAppDispatch()
  const [landed, setLanded] = useState<KiroCliContinueResult | null>(null)
  const pages = useInfiniteQuery({
    queryKey: ['kiro-cli-session', sessionId],
    queryFn: ({ pageParam }) => api.kiroCliSession(sessionId, pageParam, PAGE),
    initialPageParam: undefined as number | undefined,
    // Pages run newest to oldest; a page starting at 0 is the first message.
    getNextPageParam: (last: KiroCliSessionPage) => (last.start > 0 ? last.start : undefined),
  })
  const open = (r: KiroCliContinueResult) => {
    dispatch(switchSlot({ key: r.key, keepTargetOnMissing: true }))
    dispatch(requestSlotFolderOpen({ key: r.key, folder_id: r.folder_id }))
    onClose()
  }
  const continueMutation = useMutation({
    mutationFn: () => api.continueKiroCliSession(sessionId),
    onSuccess: async (r) => {
      // Listed before the refresh, as an import is: a slots frame built before
      // the copy landed would otherwise drop the new key.
      dispatch(addSlotOptimistic({ key: r.key, title: r.title || r.key, messages: r.messages || 0, running: false, ...(r.folder_id ? { folder_id: r.folder_id } : {}) }))
      await dispatch(fetchSlots())
      if (r.project_skipped) setLanded(r)
      else open(r)
    },
  })

  const first = pages.data?.pages[0]
  // Oldest page first so the transcript reads top to bottom.
  const messages = (pages.data?.pages ?? []).slice().reverse().flatMap(p => p.messages)
  const busy = continueMutation.isPending
  const describedBy = useId()

  return (
    <DialogContent maxWidth={720} aria-describedby={describedBy} data-testid="kiro-cli-preview">
      <DialogHeader>
        <div className="flex min-w-0 flex-col gap-0.5">
          <DialogTitle>{first?.title || i18nT('components.kiroCliSessionPreview.title')}</DialogTitle>
          <DialogDescription id={describedBy} className="flex min-w-0 items-center gap-1.5">
            <FolderOpen size={13} className="shrink-0" aria-hidden="true" />
            <span className="truncate" title={first?.cwd || undefined}>{first?.cwd || i18nT('components.kiroCliSessionPreview.no_folder')}</span>
            {first?.updated_at && <span className="shrink-0">· {fmtRelativeTime(first.updated_at)}</span>}
          </DialogDescription>
        </div>
      </DialogHeader>
      <DialogBody>
        <div className="flex flex-col gap-3" data-testid="kiro-cli-preview-transcript">
          {pages.isPending && (
            <p className="m-0 text-[13px] text-muted" role="status">{i18nT('components.kiroCliSessionPreview.loading')}</p>
          )}
          <ErrorNotice message={pages.error ? errMessage(pages.error) : null} askAgent testId="kiro-cli-preview-error" />
          {pages.hasNextPage && (
            <Btn className="self-center" onClick={() => void pages.fetchNextPage()} disabled={pages.isFetchingNextPage} data-testid="kiro-cli-preview-earlier">
              {i18nT('components.kiroCliSessionPreview.show_earlier')}
            </Btn>
          )}
          {first && messages.length === 0 && (
            <p className="m-0 text-[13px] text-muted">{i18nT('components.kiroCliSessionPreview.no_messages')}</p>
          )}
          {messages.map((m, i) => (
            <div
              key={`${first?.total ?? 0}-${i}`}
              className={`rounded-lg px-3 py-2 text-[13px] leading-relaxed ${m.role === 'user' ? 'bg-accent-subtle self-end max-w-[85%]' : 'bg-bg-hover self-start max-w-full'}`}
            >
              <div className="text-[11px] font-semibold text-muted mb-0.5">
                {m.role === 'user' ? i18nT('components.kiroCliSessionPreview.you') : i18nT('components.kiroCliSessionPreview.kiro')}
              </div>
              <div className="whitespace-pre-wrap break-words text-text">{m.content}</div>
              {m.truncated && (
                <div className="mt-1 text-[11px] text-muted">{i18nT('components.kiroCliSessionPreview.truncated')}</div>
              )}
            </div>
          ))}
          {first && first.skipped > 0 && (
            <p className="m-0 text-[12px] text-muted">{i18nT('components.kiroCliSessionPreview.skipped')}</p>
          )}
          {landed && (
            <div role="status" className="rounded-lg border border-border px-3 py-2 text-[13px]" data-testid="kiro-cli-preview-landed">
              {i18nT('components.kiroCliSessionPreview.opened_without_project', { reason: landed.project_skipped ?? '' })}
            </div>
          )}
          <p className="m-0 text-[12px] text-muted">{i18nT('components.kiroCliSessionPreview.continue_hint')}</p>
          <ErrorNotice
            message={continueMutation.error ? errMessage(continueMutation.error) : null}
            title={i18nT('components.kiroCliSessionPreview.continue_failed')}
            askAgent
            testId="kiro-cli-preview-continue-error"
          />
        </div>
      </DialogBody>
      <DialogFooter className="flex-wrap">
        <Btn onClick={onClose} data-testid="kiro-cli-preview-close">{i18nT('components.kiroCliSessionPreview.close')}</Btn>
        {landed ? (
          <Btn primary onClick={() => open(landed)} data-testid="kiro-cli-preview-open">
            {i18nT('components.kiroCliSessionPreview.open_chat')}
          </Btn>
        ) : (
          <Btn
            primary
            onClick={() => continueMutation.mutate()}
            disabled={busy || !first || messages.length === 0}
            data-testid="kiro-cli-preview-continue"
          >
            {busy && <Loader2 size={13} className="animate-spin" aria-hidden="true" />}
            {busy ? i18nT('components.kiroCliSessionPreview.continuing') : i18nT('components.kiroCliSessionPreview.continue')}
          </Btn>
        )}
      </DialogFooter>
    </DialogContent>
  )
}
