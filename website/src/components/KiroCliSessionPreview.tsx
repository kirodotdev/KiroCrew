import { useId, useState } from 'react'
import { useInfiniteQuery, useMutation } from '@tanstack/react-query'
import { FolderOpen, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import type { KiroCliContextSkipped, KiroCliContinueResult, KiroCliSessionPage } from '../api/client/sessions'
import { useAppDispatch, useAppStore } from '../store'
import { addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import { captureForkOrigin, forkMayOpen, requestSlotFolderOpen, switchSlot, type ForkOrigin } from '../store/chatSlice'
import { errMessage } from '../utils/thunkError'
import { fmtRelativeTime } from '../pages/chat/sessionOrder'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { i18nT } from '../i18n/t'

/** Messages per page of the preview; the gateway caps a page at 500. */
const PAGE = 100

/** What the new chat keeps, said before Continue: one fixed sentence per code
 *  the gateway answers, naming the setting that would change it, if any. */
const STATE_LINE: Record<KiroCliContextSkipped | 'kept', () => string> = {
  kept: () => i18nT('components.kiroCliSessionPreview.state_kept'),
  tool_search_replays_transcript: () => i18nT('components.kiroCliSessionPreview.state_needs_tool_search_off'),
  kiro_cli_backend_required: () => i18nT('components.kiroCliSessionPreview.state_needs_harness'),
  kiro_cli_state_unavailable: () => i18nT('components.kiroCliSessionPreview.state_unavailable'),
}

/**
 * A read-only look at one kiro-cli session a person ran in a terminal, with the
 * one action it offers: Continue in Crew.
 *
 * The gateway serves the conversation newest page first; "Show earlier messages"
 * walks back a page at a time. Nothing here can change the terminal session:
 * Continue COPIES it into a new chat (`POST /api/kiro-cli/sessions/{id}/continue`).
 * The chat carries kiro-cli's own session state only where the gateway says it
 * can (the preview page's `context_skipped` is absent); otherwise it continues
 * from the transcript, which is the expected result on the default setup and not
 * a failure. The line under the transcript says which, before the click.
 *
 * On success the new chat opens and the dialog closes, a transcript-only copy
 * included: its title already says "transcript only". When the session's
 * folder could not be used as the chat's project (it was deleted, or overlaps
 * Crew's own data), the dialog stays open long enough to say so, because the
 * chat would otherwise open in a different folder with no sign of why.
 *
 * *onOpened* is the host's own "a chat was selected" hand-off (the embedded
 * sessions list navigates to the chat through it), called whenever this opens
 * the new chat. A chat the person opened while the copy was being made wins,
 * as it does for an import: the copy is then only listed, and an open dialog
 * offers "Open the chat" instead of opening it.
 *
 * The dialog cannot be dismissed while the copy is being made (Close, the X,
 * Escape and the backdrop all wait): it owns the request, so closing it would
 * leave a refusal with nowhere to be shown.
 */
export default function KiroCliSessionPreview({ sessionId, onClose, onOpened }: { sessionId: string | null; onClose: () => void; onOpened?: (key: string) => void }) {
  return (
    <Dialog open={sessionId !== null} onOpenChange={open => { if (!open) onClose() }}>
      {sessionId !== null && <PreviewContent key={sessionId} sessionId={sessionId} onClose={onClose} onOpened={onOpened} />}
    </Dialog>
  )
}

function PreviewContent({ sessionId, onClose, onOpened }: { sessionId: string; onClose: () => void; onOpened?: (key: string) => void }) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const [landed, setLanded] = useState<KiroCliContinueResult | null>(null)
  const pages = useInfiniteQuery({
    queryKey: ['kiro-cli-session', sessionId],
    queryFn: ({ pageParam }) => api.kiroCliSession(sessionId, pageParam, PAGE),
    initialPageParam: undefined as number | undefined,
    // Pages run newest to oldest; a page starting at 0 is the first message.
    getNextPageParam: (last: KiroCliSessionPage) => (last.start > 0 ? last.start : undefined),
    // Read again on every open: turns typed in the terminal since the last
    // look reach no Crew event that could invalidate this.
    staleTime: 0,
    refetchOnMount: 'always',
  })
  // *later* is the "Open the chat" click after the folder notice: the copy
  // landed a while ago and may have been closed since, so that click announces
  // a missing chat instead of waiting for one that is still being created.
  const open = (r: KiroCliContinueResult, later = false) => {
    if (later) dispatch(switchSlot({ key: r.key, announceOnMissing: true, gesture: true }))
    else dispatch(switchSlot({ key: r.key, keepTargetOnMissing: true }))
    dispatch(requestSlotFolderOpen({ key: r.key, folder_id: r.folder_id }))
    if (store.getState().chat.activeSlot === r.key) onOpened?.(r.key)
    onClose()
  }
  const continueMutation = useMutation({
    // `origin` is where the person stood when they chose Continue; see onSuccess.
    mutationFn: (_origin: ForkOrigin) => api.continueKiroCliSession(sessionId),
    onSuccess: async (r, origin) => {
      // Listed before the refresh, as an import is: a slots frame built before
      // the copy landed would otherwise drop the new key.
      dispatch(addSlotOptimistic({ key: r.key, title: r.title || r.key, messages: r.messages || 0, running: false, ...(r.folder_id ? { folder_id: r.folder_id } : {}) }))
      await dispatch(fetchSlots())
      // A chat opened while the copy was made is a choice this must not undo.
      // A transcript-only copy (`context_skipped`) is the expected outcome on the
      // default setup, not a failure, so it opens like any other.
      if (r.project_skipped || !forkMayOpen(store.getState(), origin)) setLanded(r)
      else open(r)
    },
  })

  const first = pages.data?.pages[0]
  // Oldest page first so the transcript reads top to bottom.
  const messages = (pages.data?.pages ?? []).slice().reverse().flatMap(p => p.messages)
  const busy = continueMutation.isPending
  const describedBy = useId()

  return (
    <DialogContent
      maxWidth={720}
      aria-describedby={describedBy}
      data-testid="kiro-cli-preview"
      hideClose={busy}
      onEscapeKeyDown={e => { if (busy) e.preventDefault() }}
      onPointerDownOutside={e => { if (busy) e.preventDefault() }}
    >
      <DialogHeader>
        <div className="flex min-w-0 flex-col gap-0.5">
          <DialogTitle>{first?.title || i18nT('components.kiroCliSessionPreview.title')}</DialogTitle>
          <DialogDescription id={describedBy} className="min-w-0">
            {/* The row's own layout lives on this span: the description owns its spacing. */}
            <span className="flex min-w-0 items-center gap-1.5">
              <FolderOpen size={13} className="shrink-0" aria-hidden="true" />
              <span className="truncate" title={first?.cwd || undefined}>{first?.cwd || i18nT('components.kiroCliSessionPreview.no_folder')}</span>
              {first?.updated_at && <span className="shrink-0">· {fmtRelativeTime(first.updated_at)}</span>}
            </span>
          </DialogDescription>
        </div>
      </DialogHeader>
      <DialogBody>
        <div className="flex flex-col gap-3" data-testid="kiro-cli-preview-transcript">
          {pages.isPending && (
            <p className="m-0 text-[13px] text-muted" role="status">{i18nT('components.kiroCliSessionPreview.loading')}</p>
          )}
          <ErrorNotice message={pages.error ? errMessage(pages.error) : null} askAgent onHandoff={onClose} testId="kiro-cli-preview-error" />
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
          {/* A degraded read and a folder that could not be used are failures the
              gateway reported, so they go through ErrorNotice like the refusals. */}
          <ErrorNotice
            message={first && first.skipped > 0 ? i18nT('components.kiroCliSessionPreview.skipped') : null}
            askAgent
            onHandoff={onClose}
            testId="kiro-cli-preview-skipped"
          />
          <ErrorNotice
            message={landed?.project_skipped ? i18nT('components.kiroCliSessionPreview.opened_without_project', { reason: landed.project_skipped }) : null}
            title={i18nT('components.kiroCliSessionPreview.opened_without_folder')}
            askAgent
            onHandoff={onClose}
            testId="kiro-cli-preview-landed"
          />
          {/* What the new chat keeps, before the click and after it: a
              transcript-only copy is a working fallback, not a failure, so this
              is a plain muted line rather than an ErrorNotice. The landed result
              wins once there is one; each code gets one fixed sentence and no
              configured value is shown. */}
          <div className="flex flex-col gap-1 text-[12px] text-muted" data-testid="kiro-cli-preview-hint">
            <p className="m-0">{i18nT('components.kiroCliSessionPreview.continue_hint')}</p>
            {first && (
              <p className="m-0" data-testid="kiro-cli-preview-state">
                {STATE_LINE[(landed ? landed.context_skipped : first.context_skipped) ?? 'kept']()}
              </p>
            )}
          </div>
          <ErrorNotice
            message={continueMutation.error ? errMessage(continueMutation.error) : null}
            title={i18nT('components.kiroCliSessionPreview.continue_failed')}
            askAgent
            onHandoff={onClose}
            testId="kiro-cli-preview-continue-error"
          />
        </div>
      </DialogBody>
      <DialogFooter className="flex-wrap">
        <Btn onClick={onClose} disabled={busy} data-testid="kiro-cli-preview-close">{i18nT('components.kiroCliSessionPreview.close')}</Btn>
        {landed ? (
          <Btn primary onClick={() => open(landed, true)} data-testid="kiro-cli-preview-open">
            {i18nT('components.kiroCliSessionPreview.open_chat')}
          </Btn>
        ) : (
          <Btn
            primary
            onClick={() => continueMutation.mutate(captureForkOrigin(store.getState()))}
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
