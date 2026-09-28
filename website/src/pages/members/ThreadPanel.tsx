/**
 * A reply thread on one message of a crewmate's chat, opened in the right side
 * panel (CrewMates launch, screen 07). The main chat stays visible beside it.
 *
 * Top to bottom: a "Thread" header with the crewmate's name and a close
 * button; the parent message quoted as a single bubble (avatar, name, time);
 * a hairline "N replies"; the replies as small bubbles on the grouped-corner
 * rule (the crewmate's on the left under a small avatar, the user's on the
 * right, mirrored); and a one-line "Reply…" composer with the real SendBtn.
 *
 * Stored replies come from `threadsApi.detail` (React Query); the crewmate's
 * reply-in-progress streams through `threadLiveStore`, fed by
 * `chat.thread_reply` frames. While the crewmate is replying the composer is
 * disabled (the backend refuses a second reply in that thread until then), and
 * a typing row stands in as an ordinary item, not a notice.
 *
 * The composer's draft is kept per thread (`threadDrafts`) so closing the
 * panel and opening the thread again finds what was typed; a sent reply clears
 * it. Escape closes the panel as the close button does, and closing hands
 * focus back to the control that opened it (the message's Reply action), so a
 * keyboard user lands where they left the chat.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, RotateCw, X } from 'lucide-react'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import { Btn, SendBtn } from '../../components/ui'
import { useImeGuard } from '../../hooks/useImeGuard'
import { ApiError } from '../../api/apiError'
import { threadQueryKey, threadsApi, threadsQueryKey, type ThreadReply } from '../../api/threads'
import { fmtMessageTime } from '../chat/messageTime'
import { threadDrafts, threadLiveStore, threadPendingReplies } from '../../state/threadLiveStore'
import { parseErrorCode } from '../../utils/errorReport'
import { crewmateBubbleClass, crewmateRunPosition, opensCrewmateRun, type CrewmateRunPosition } from '../../components/chat/crewmateBubbles'
import type { ChatMessage } from '../../types'

const AVATAR_PX = 22
// The server's cap is 32 KiB of UTF-8 (`_MAX_REPLY_BYTES`), so the client
// measures bytes too: a character cap would let a CJK reply well under it be
// posted and answered 413.
const MAX_REPLY_BYTES = 32 * 1024
const utf8Bytes = (text: string) => new TextEncoder().encode(text).length
/** A reply id in the store's own shape (uuid4 hex), minted here per send so a
 *  retry after a lost response is the SAME reply to the server, not a second one. */
const mintReplyId = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`).replace(/-/g, '').padEnd(32, '0').slice(0, 32)

/** The user's bubble: the same surface as the crewmate's, every corner full —
 *  the user's messages never group (components/chat/crewmateBubbles). */
const USER_BUBBLE = 'bg-card border border-border px-3.5 py-1.5 rounded-2xl max-w-[85%]'

/** One small bubble. The crewmate's (`side="left"`) takes the main chat's
 *  corner rule for its place in the run; the user's is always a single. */
function Bubble({ pos = 'single', side, children, testId }: { pos?: CrewmateRunPosition; side: 'left' | 'right'; children: React.ReactNode; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className={`${side === 'left' ? crewmateBubbleClass(pos) : USER_BUBBLE} text-card-fg text-[13px] leading-[1.45]`}
      style={{ overflowWrap: 'anywhere' }}
    >
      {children}
    </div>
  )
}

/** Author line above the first bubble of a run: name + time. */
function AuthorLine({ name, ts, align }: { name: string; ts: string; align: 'left' | 'right' }) {
  return (
    <span className={`text-[11px] leading-4 text-muted tabular-nums mb-1 ${align === 'left' ? 'ml-1' : 'mr-1'}`}>
      <span className="font-semibold text-text">{name}</span>
      {ts && <> · {fmtMessageTime(ts)}</>}
    </span>
  )
}

function ReplyRow({ reply, pos, crewmateName, crewmateLabel, youLabel }: { reply: ThreadReply; pos: CrewmateRunPosition; crewmateName: string; crewmateLabel?: string; youLabel: string }) {
  if (reply.role === 'user') {
    return (
      <li className="flex flex-col items-end mt-3" data-testid="thread-reply" data-reply-from="user">
        <AuthorLine name={youLabel} ts={reply.ts} align="right" />
        <Bubble side="right">{reply.content}</Bubble>
      </li>
    )
  }
  const opens = opensCrewmateRun(pos)
  return (
    <li className={`flex gap-2 ${opens ? 'mt-3' : 'mt-1'}`} data-testid="thread-reply" data-reply-from="assistant" data-thread-run={pos}>
      <div className="shrink-0" style={{ width: AVATAR_PX }}>{opens && <CrewAvatar seed={crewmateName} size={AVATAR_PX} />}</div>
      <div className="min-w-0 flex-1 flex flex-col items-start">
        {opens && <AuthorLine name={crewmateLabel || crewmateName} ts={reply.ts} align="left" />}
        <Bubble pos={pos} side="left">
          <MessageErrorBoundary rawContent={reply.content}><MarkdownRenderer content={reply.content} softBreaks /></MessageErrorBoundary>
        </Bubble>
      </div>
    </li>
  )
}

export default function ThreadPanel({ slot, mid, crewmateName, crewmateLabel, onClose }: {
  slot: string
  mid: string
  crewmateName: string
  /** Presentation label rendered in place of the name; the name still seeds
   *  avatars and keys the thread, so both are needed. */
  crewmateLabel?: string
  onClose: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const detail = useQuery({
    queryKey: threadQueryKey(slot, mid),
    queryFn: () => threadsApi.detail(slot, mid),
  })
  const live = useSyncExternalStore(
    useCallback((cb: () => void) => threadLiveStore.subscribe(slot, mid, cb), [slot, mid]),
    () => threadLiveStore.get(slot, mid),
  )
  // The draft outlives the panel: read back from the per-thread store on mount,
  // written through on every keystroke, cleared by a sent reply.
  const [draft, setDraftState] = useState(() => threadDrafts.get(slot, mid))
  const setDraft = useCallback((text: string) => {
    setDraftState(text)
    threadDrafts.set(slot, mid, text)
  }, [slot, mid])
  const ime = useImeGuard()
  const rootRef = useRef<HTMLDivElement>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Where focus was when the panel opened -- the Reply action on the message --
  // so closing returns it there. Captured before the composer takes focus.
  const openerRef = useRef<HTMLElement | null>(null)

  // The id of the send in flight or the one that just failed, with the text it
  // was minted for: a retry of the SAME text reuses it, so a reply whose 202 was
  // lost on the wire is handed back by the server instead of stored twice; an
  // edited text is a new reply and gets a new id. Kept in the per-thread store,
  // not in this component, so closing and reopening the panel over a kept draft
  // does not mint a fresh id for the retry.
  const send = useMutation({
    mutationFn: (text: string) => {
      const pending = threadPendingReplies.get(slot, mid)
      const id = pending && pending.text === text ? pending.id : mintReplyId()
      threadPendingReplies.set(slot, mid, { id, text })
      return threadsApi.reply(slot, mid, text, id)
    },
    onMutate: () => threadLiveStore.clearError(slot, mid),
    onSuccess: (_reply, sent) => {
      threadPendingReplies.set(slot, mid, null)
      // Clear the composer only if it still holds the text that was sent: the
      // textarea stays live during the send, so words typed meanwhile are the
      // NEXT reply and must not go with the acknowledgement of this one.
      if (threadDrafts.get(slot, mid).trim() === sent) setDraft('')
      void qc.invalidateQueries({ queryKey: threadQueryKey(slot, mid) })
      void qc.invalidateQueries({ queryKey: threadsQueryKey(slot) })
    },
  })

  const storedReplies = detail.data?.replies
  const replies = useMemo(() => storedReplies ?? [], [storedReplies])
  // The crewmate's consecutive replies group on the main chat's run rule, read
  // over the replies as transcript rows; the user's never do.
  const positions = useMemo(() => {
    const rows: ChatMessage[] = replies.map((r) => ({ role: r.role, content: r.content, cls: '', ts: r.ts }))
    return rows.map((row, i) => (row.role === 'assistant' ? crewmateRunPosition(rows, i) : 'single'))
  }, [replies])
  // The crewmate is writing: the server says so, or a streamed delta is in hand.
  const replying = !!detail.data?.in_flight || (!!live && !live.error)
  const youLabel = t('pages.chat.thread.you')

  // Follow the tail as replies and streamed text land.
  useEffect(() => {
    const el = scrollerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [replies.length, live?.text])
  // Escape closes, as the close button does -- the same panel-level listener
  // ActivityViewer uses. Not while an IME composition is open: Escape then
  // cancels the composition, not the panel. Stopped here so the overlay panel
  // behind does not also read it as its own dismissal.
  useEffect(() => {
    const el = rootRef.current
    if (!el) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return
      e.preventDefault()
      e.stopPropagation()
      onClose()
    }
    el.addEventListener('keydown', handler)
    return () => el.removeEventListener('keydown', handler)
  }, [onClose])
  useEffect(() => {
    const opener = document.activeElement
    openerRef.current = opener instanceof HTMLElement && opener !== document.body ? opener : null
    textareaRef.current?.focus()
    return () => {
      // Unmount = the thread closed (or another one replaced it): hand focus
      // back to the opener if it is still on the page, so the user is not
      // dropped at the document root.
      const back = openerRef.current
      if (back && back.isConnected) back.focus()
    }
  }, [mid])

  const tooLong = utf8Bytes(draft) > MAX_REPLY_BYTES
  // A thread that failed to load has no visible replies to reply to; the composer
  // waits for Retry rather than taking a blind reply into content the user cannot
  // see. The draft is kept meanwhile.
  const unreadable = detail.isError
  // Disabling the composer drops the focus it held; keep the keyboard inside
  // the panel (Escape still closes) by moving it to the Retry the notice offers.
  useEffect(() => {
    if (!unreadable) return
    const root = rootRef.current
    const active = document.activeElement
    const heldInside =
      active instanceof HTMLElement && active !== document.body && root?.contains(active) &&
      !(active as HTMLElement & { disabled?: boolean }).disabled
    if (!root || heldInside) return
    root.querySelector<HTMLElement>('[data-testid="thread-load-retry"]')?.focus()
  }, [unreadable])
  const submit = () => {
    const text = draft.trim()
    if (!text || tooLong || send.isPending || replying || unreadable) return
    send.mutate(text)
  }

  // One plain sentence per failure; the backend's code picks the sentence and a
  // failed send keeps the draft, so nothing typed is lost.
  const sendError = (() => {
    const err = send.error
    if (!err) return ''
    const code = err instanceof ApiError ? parseErrorCode(err.body) : undefined
    if (code === 'thread_turn_in_flight') return t('pages.chat.thread.err_replying', { name: crewmateLabel || crewmateName })
    if (code === 'parent_not_found') return t('pages.chat.thread.err_parent_gone')
    if (code === 'reply_too_long') return t('pages.chat.thread.err_too_long')
    if (code === 'thread_full') return t('pages.chat.thread.err_full')
    if (code === 'threads_full') return t('pages.chat.thread.err_chat_full')
    return t('pages.chat.thread.err_send_failed')
  })()
  // An over-long draft is said before the send, with the same sentence the
  // server would answer; the send button stays disabled until it is shortened.
  // Only real failures reach ErrorNotice. An over-long draft is a validation
  // hint, not an error: nothing was sent, so it is said as muted text beside
  // the composer while the send button stays disabled on `tooLong`.
  const shownError = sendError || live?.error || ''

  const parent = detail.data?.parent
  const parentIsUser = parent?.role === 'user'

  return (
    <div
      data-testid="thread-panel"
      className="absolute inset-0 z-20 flex flex-col bg-bg"
      role="complementary"
      aria-label={t('pages.chat.thread.title')}
      ref={rootRef}
    >
      <div className="shrink-0 flex items-center gap-2 px-3 min-h-10 rounded-tl-xl bg-bg-elevated border-b border-border">
        <h2 className="text-[13px] font-semibold m-0 leading-none">{t('pages.chat.thread.title')}</h2>
        <span className="text-[12px] text-muted truncate">{crewmateLabel || crewmateName}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto inline-flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          aria-label={t('pages.chat.thread.close')}
          title={t('pages.chat.thread.close')}
        >
          <X className="lucide-inline" style={{ width: 15, height: 15 }} />
        </button>
      </div>
      <div ref={scrollerRef} className="flex-1 min-h-0 overflow-y-auto px-3 pt-3">
        {detail.isError && (
          /* No hand-off: the reply draft in the composer below is unsaved local
             state; a navigation would discard it. Retry re-runs the read in
             place; while it runs the button is disabled, and the failed read is
             also retried by the next chat.thread_reply frame. */
          <div className="flex items-start gap-2 mb-2">
            <ErrorNotice
              variant="inline"
              message={t('pages.chat.thread.err_load_failed')}
              className="flex-1 min-w-0"
              testId="thread-load-error"
            />
            <Btn
              disabled={detail.isFetching}
              onClick={() => { void detail.refetch() }}
              className="shrink-0"
              data-testid="thread-load-retry"
            >
              <RotateCw className="lucide-inline" aria-hidden />
              {t('pages.chat.thread.retry')}
            </Btn>
          </div>
        )}
        {parent && (
          parentIsUser ? (
            <div className="flex flex-col items-end" data-testid="thread-parent">
              <AuthorLine name={youLabel} ts={parent.ts} align="right" />
              <Bubble side="right" testId="thread-parent-bubble">{parent.content}</Bubble>
            </div>
          ) : (
            <div className="flex gap-2" data-testid="thread-parent">
              <div className="shrink-0" style={{ width: AVATAR_PX }}><CrewAvatar seed={crewmateName} size={AVATAR_PX} /></div>
              <div className="min-w-0 flex-1 flex flex-col items-start">
                <AuthorLine name={crewmateLabel || crewmateName} ts={parent.ts} align="left" />
                <Bubble side="left" testId="thread-parent-bubble">
                  <MessageErrorBoundary rawContent={parent.content}><MarkdownRenderer content={parent.content} softBreaks /></MessageErrorBoundary>
                </Bubble>
              </div>
            </div>
          )
        )}
        {parent && (
          <div className="flex items-center gap-2 my-3 text-[11px] text-muted" data-testid="thread-reply-count">
            <span className="shrink-0">
              {replies.length > 0
                ? t('pages.chat.thread.replies_count', { count: replies.length })
                : t('pages.chat.thread.no_replies_yet')}
            </span>
            <span className="flex-1 h-px bg-border" aria-hidden="true" />
          </div>
        )}
        <ul className="list-none m-0 p-0" data-testid="thread-replies">
          {replies.map((r, i) => (
            <ReplyRow key={r.id} reply={r} pos={positions[i] ?? 'single'} crewmateName={crewmateName} crewmateLabel={crewmateLabel} youLabel={youLabel} />
          ))}
          {live && !live.error && (
            // The reply as it streams in: one crewmate bubble that grows, then
            // gives way to the stored row the terminal frame refetches.
            <li className="flex gap-2 mt-3" data-testid="thread-reply-live" aria-live="polite">
              <div className="shrink-0" style={{ width: AVATAR_PX }}><CrewAvatar seed={crewmateName} size={AVATAR_PX} working="subtle" /></div>
              <div className="min-w-0 flex-1 flex flex-col items-start">
                <AuthorLine name={crewmateLabel || crewmateName} ts="" align="left" />
                <Bubble side="left">
                  <MessageErrorBoundary rawContent={live.text}><MarkdownRenderer content={live.text} streaming softBreaks /></MessageErrorBoundary>
                </Bubble>
              </div>
            </li>
          )}
          {replying && !live?.text && (
            <li className="flex items-center gap-1.5 mt-3 px-1 text-muted" data-testid="thread-replying" role="status">
              <span className="flex gap-0.5" aria-hidden="true">
                <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '300ms' }} />
              </span>
              <span className="text-[12px]">{t('pages.chat.thread.replying', { name: crewmateLabel || crewmateName })}</span>
            </li>
          )}
        </ul>
      </div>
      {tooLong && (
        <p className="px-3 pt-1 text-[12px] text-muted" data-testid="thread-too-long" role="status">
          {t('pages.chat.thread.err_too_long')}
        </p>
      )}
      {shownError && (
        <div className="px-3 pt-1">
          {/* No hand-off: the draft below is unsaved; a failed send keeps it. */}
          <ErrorNotice variant="inline" message={shownError} testId="thread-send-error" />
        </div>
      )}
      <div className="shrink-0 px-3 pb-3 pt-2">
        <div
          className={
            'flex items-center gap-2 rounded-xl border border-border focus-within:border-accent bg-card px-3 py-2 transition-colors' +
            // The wait-for-Retry rule is shown, not only enforced: a muted card
            // and a placeholder that names the next step.
            (unreadable ? ' opacity-60 cursor-not-allowed' : '')
          }
          aria-disabled={unreadable || undefined}
        >
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            {...ime.bindComposition()}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { if (ime.claimEnter(e)) submit() }
            }}
            className={/* focus-cue-ok: the cue is the composer card's focus-within border-accent, the same cue the main composer shell paints; a second ring on the textarea would double-paint one control. */ 'flex-1 min-w-0 resize-none bg-transparent text-[13px] leading-6 text-card-fg outline-hidden placeholder:text-muted'}
            placeholder={t(unreadable ? 'pages.chat.thread.reply_after_retry' : 'pages.chat.thread.reply_placeholder')}
            aria-label={t('pages.chat.thread.reply_in_thread')}
            data-testid="thread-composer"
            disabled={unreadable}
          />
          <SendBtn
            className="px-0 min-h-0 w-8 h-8 rounded-full inline-flex items-center justify-center shrink-0"
            aria-label={t('pages.chat.thread.send_reply')}
            disabled={!draft.trim() || tooLong || send.isPending || replying || unreadable}
            onClick={submit}
          >
            <ArrowUp className="lucide-inline" style={{ width: 18, height: 18 }} />
          </SendBtn>
        </div>
      </div>
    </div>
  )
}
