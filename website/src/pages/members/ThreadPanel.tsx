/**
 * One thread, in the right side panel of whatever chat it hangs off. The main
 * chat stays visible beside it.
 *
 * A thread is an ordinary chat session anchored to one message (NOTES D1), so
 * this panel is a frame around the ORDINARY chat pane rather than a bubble list
 * of its own: header, the anchored message quoted once, then `ChatPane` on the
 * thread's own slot. Everything a chat surface has comes with it — a real
 * composer, many turns, tools, approval cards, steer, queue, stop, model and
 * agent pickers, history. None of it is reimplemented here, and there is no
 * one-reply lock and no client byte cap, because those existed only to keep a
 * throwaway stateless session honest.
 *
 * The same slot is also reachable as an ordinary full page (`/chat/<thread
 * slot>`); the pop-out control in the header goes there. Both are the same
 * session, so leaving one for the other loses nothing.
 *
 * Version 1 threads — written when a thread's replies lived in a sidecar — have
 * no session to render. They arrive with `replies` and no `thread_slot`, and
 * are drawn as a read-only fold under the anchor with one action: start a real
 * thread here (NOTES D3).
 *
 * Escape closes the panel as the close button does, and closing hands focus
 * back to the control that opened it, so a keyboard user lands where they left
 * the chat.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { Maximize2, RotateCw, X } from 'lucide-react'
import ChatPane from '../../components/ChatPane'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import { Btn } from '../../components/ui'
import { threadQueryKey, threadsApi, type ThreadReply } from '../../api/threads'
import { fmtMessageTime } from '../chat/messageTime'
import { threadTitleBeside } from '../chat/threadTitle'
import { threadLiveStore } from '../../state/threadLiveStore'
import { useAppSelector } from '../../store'
import { selectSlotMessages, selectSlotStreamState } from '../../store/chatSlice'
import { crewmateBubbleClass, crewmateRunPosition, opensCrewmateRun, type CrewmateRunPosition } from '../../components/chat/crewmateBubbles'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'
import type { ChatMessage } from '../../types'

const AVATAR_PX = 22
/** One identity for "no version 1 replies", so the memo below is not
 *  invalidated by a fresh empty array every render. */
const EMPTY_REPLIES: readonly ThreadReply[] = []

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

function LegacyReplyRow({ reply, pos, crewmateName, crewmateLabel, youLabel }: { reply: ThreadReply; pos: CrewmateRunPosition; crewmateName: string; crewmateLabel?: string; youLabel: string }) {
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

export default function ThreadPanel({
  slot,
  mid,
  threadSlot: threadSlotProp,
  crewmateName,
  crewmateLabel,
  crewmate,
  onClose,
  onOpenFull,
  onStartNew,
  startingNew,
  onEnd,
  ending,
  endError,
}: {
  /** The PARENT conversation's slot: half of the anchor. */
  slot: string
  /** The anchored message's id: the other half. */
  mid: string
  /** The thread's own slot, when the host already knows it (it just opened one).
   *  Otherwise it is read from the anchor. */
  threadSlot?: string
  crewmateName: string
  /** Presentation label rendered in place of the name; the name still seeds
   *  avatars and keys the thread, so both are needed. */
  crewmateLabel?: string
  /** Passed straight to the pane when the thread belongs to a crewmate's chat,
   *  so its transcript reads the way the parent's does. Absent on an ordinary
   *  chat, where the thread is an ordinary transcript. */
  crewmate?: CrewmateIdentity
  onClose: () => void
  /** Leave the drawer for the thread's own full page. Absent = no pop-out
   *  offered (capability by omission, as ChatPane's `onOpenFull` is). */
  onOpenFull?: (threadSlot: string) => void
  /** Mint a real thread on this anchor — the one action a version 1 fold offers. */
  onStartNew?: () => void
  startingNew?: boolean
  /** End the thread: close its anchor and post the summary card in the parent.
   *  Absent = not offered, which is the case for a version 1 fold (no session to
   *  end) and for a thread already closed. Distinct from `onClose`, which only
   *  dismisses this panel. */
  onEnd?: () => void
  ending?: boolean
  /** An i18n key for a refused end, or '' when nothing failed. */
  endError?: string
}) {
  const { t } = useTranslation()
  const detail = useQuery({
    queryKey: threadQueryKey(slot, mid),
    queryFn: () => threadsApi.detail(slot, mid),
  })
  // An anchor announcement for this message, from this or another tab.
  const live = useSyncExternalStore(
    useCallback((cb: () => void) => threadLiveStore.subscribe(slot, mid, cb), [slot, mid]),
    () => threadLiveStore.get(slot, mid),
  )
  // Three sources, and the host's prop is NOT simply the freshest. It is captured
  // when the panel opens and never changes again, so on a message whose thread was
  // closed and reopened elsewhere -- another tab, an agent -- it names the ENDED
  // session while the store and the anchor read both name the replacement. Trusting
  // it then keeps the pane mounted on a finished transcript and sends into it.
  //
  // So a replacement supersedes the prop: when either live source names a different
  // slot for this same message, that slot wins. The prop still leads when they agree
  // or say nothing, which is the ordinary case and the one that avoids a flash of
  // empty pane while the anchor read is still in flight.
  const anchor = detail.data?.anchor
  const announced = live?.threadSlot || anchor?.thread_slot || ''
  const threadSlot =
    announced && threadSlotProp && announced !== threadSlotProp
      ? announced
      : threadSlotProp || announced || ''
  const closedAt = anchor?.closed_at || live?.closedAt || ''
  const threadTitle = anchor?.title || live?.title || ''
  // The PARENT's still-streaming text, or '' when its turn is not running. Read
  // straight off the parent slot's own rows: `applyNonActiveFrame` appends them for
  // a slot that is not the active one, which is exactly this case, so the mirror
  // needs no second subscription and cannot drift from the main chat.
  const parentStreaming = useAppSelector((s) => selectSlotStreamState(s, slot))
  const parentRows = useAppSelector((s) => selectSlotMessages(s, slot))
  const parentLive = useMemo(() => {
    if (parentStreaming !== 'streaming') return ''
    for (let i = parentRows.length - 1; i >= 0; i--) {
      if (parentRows[i].role === 'streaming') return parentRows[i].content || ''
    }
    return ''
  }, [parentStreaming, parentRows])
  // This panel is showing version 1 replies and no session: known only once the
  // anchor read has SETTLED without naming a slot. While it is in flight the panel
  // is titled "Thread", because a heading that reads "Earlier replies" for a moment
  // and then changes teaches the reader the two words mean the same thing -- which
  // is the confusion the split exists to remove.
  const showsFold = !threadSlot && detail.isSuccess
  const storedLegacy = detail.data?.legacy_replies
  const legacyReplies = useMemo(() => storedLegacy ?? EMPTY_REPLIES, [storedLegacy])

  const rootRef = useRef<HTMLDivElement>(null)
  // Where focus was when the panel opened -- the Reply action on the message --
  // so closing returns it there. Captured before anything inside takes focus.
  const openerRef = useRef<HTMLElement | null>(null)

  const youLabel = t('pages.chat.thread.you')
  // A version 1 reply run groups on the main chat's rule, as it did.
  const legacyPositions = useMemo(() => {
    const rows: ChatMessage[] = legacyReplies.map((r) => ({ role: r.role, content: r.content, cls: '', ts: r.ts }))
    return rows.map((row, i) => (row.role === 'assistant' ? crewmateRunPosition(rows, i) : 'single'))
  }, [legacyReplies])

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
    return () => {
      // Unmount = the thread closed (or another one replaced it): hand focus
      // back to the opener if it is still on the page, so the user is not
      // dropped at the document root.
      const back = openerRef.current
      if (back && back.isConnected) back.focus()
    }
  }, [mid])

  const parent = detail.data?.parent
  const parentIsUser = parent?.role === 'user'
  // The quoted anchor is COLLAPSED by default, and the region holding it is
  // bounded whatever it contains. Both are load-bearing rather than tidy: the
  // anchor sat in a `shrink-0` box with no cap, so a thread opened on a long
  // reply grew it past the panel (measured: 5 242 px of anchor in a 940 px
  // drawer) and `thread-surface` -- the thread's own transcript AND its composer
  // -- was left 0 px tall with the composer scrolled off. Nothing could be typed,
  // so a thread on any long message was not merely ugly but unusable. The cap is
  // what guarantees the surface a share of the panel; the collapse is what keeps
  // the common case from being a scroll box. Reading the whole message is a click.
  const [anchorExpanded, setAnchorExpanded] = useState(false)
  // Collapse again when the panel moves to another message: expanding is a choice
  // about ONE anchor, and carrying it across would spring the same trap on a
  // thread the reader opened on something long.
  useEffect(() => { setAnchorExpanded(false) }, [mid])
  // A thread opened on a reply STILL BEING WRITTEN anchors to the user message
  // that started that turn, because a streaming row has no `mid` to hang an
  // anchor off yet -- that part is deliberate and durable. What was wrong was the
  // reading order: the reader pressed the opener under an ANSWER and the panel
  // led with their own question, so the pane's first line was the thing they did
  // not click. The mirror already carries the reply's text; leading with it makes
  // the panel show what was pressed and keeps the question underneath as the
  // context it is. Only when the anchor IS the user row -- a thread on a finished
  // reply quotes that reply, and there is nothing to reorder.
  const leadWithLive = !!parentLive && parentIsUser

  return (
    <div
      data-testid="thread-panel"
      className="absolute inset-0 z-20 flex flex-col bg-bg"
      role="complementary"
      aria-label={showsFold ? t('pages.chat.thread.legacy_title') : t('pages.chat.thread.title')}
      ref={rootRef}
    >
      <div className="shrink-0 flex items-center gap-2 px-3 min-h-10 rounded-tl-xl bg-bg-elevated border-b border-border">
        {/* "Thread" names a live session and only that. A version 1 fold is a set
            of replies with no session behind it, so the panel showing one is
            headed "Earlier replies" outright: heading them both "Thread" made the
            word mean two things, and a reader who meets the fold first carries the
            wrong meaning into every real thread after it. */}
        <h2 className="text-[13px] font-semibold m-0 leading-none">
          {showsFold ? t('pages.chat.thread.legacy_title') : t('pages.chat.thread.title')}
        </h2>
        {threadSlot && (
          /* Stripped of its own "Thread:" prefix, because the heading beside it
             already says the word: an untitled thread stores "Thread: <first
             words>", and the two rendered together read "Thread  Thread: Amazon". */
          <span className="text-[12px] text-muted truncate">
            {threadTitleBeside(threadTitle) || crewmateLabel || crewmateName}
          </span>
        )}
        {closedAt && (
          <span className="text-[11px] text-muted border border-border rounded px-1.5 py-0.5 shrink-0" data-testid="thread-closed">
            {t('pages.chat.thread.closed')}
          </span>
        )}
        {onOpenFull && threadSlot && (
          /* The same session as a full page. Offered only once the thread has a
             slot: a version 1 fold has no session to open, so the control is
             absent rather than drawn inert. */
          <button
            type="button"
            onClick={() => onOpenFull(threadSlot)}
            className="ml-auto inline-flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
            aria-label={t('pages.chat.thread.open_full')}
            title={t('pages.chat.thread.open_full')}
            data-testid="thread-open-full"
          >
            <Maximize2 className="lucide-inline" style={{ width: 14, height: 14 }} />
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className={`${onOpenFull && threadSlot ? '' : 'ml-auto '}inline-flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer`}
          aria-label={t('pages.chat.thread.close')}
          title={t('pages.chat.thread.close')}
        >
          <X className="lucide-inline" style={{ width: 15, height: 15 }} />
        </button>
      </div>

      {((onEnd && threadSlot && !closedAt) || endError) && (
        /* Ending the thread is NOT dismissing this panel: it closes the anchor,
           releases the message to carry a later thread, and returns the result to
           the parent as a summary card. It sits on its own row rather than beside
           the two icon buttons above, which are already the header's action row
           (`max-two-buttons-per-row`), and it sits next to its own refusal so a
           failed end is answered where the control that failed is. */
        <div className="shrink-0 flex items-center gap-2 px-3 py-1.5 border-b border-border">
          {onEnd && threadSlot && !closedAt && (
            <button
              type="button"
              onClick={onEnd}
              disabled={!!ending}
              className="shrink-0 inline-flex items-center h-6 px-2 rounded-md text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-50 disabled:cursor-default"
              aria-label={t('pages.chat.thread.end')}
              data-testid="thread-end"
            >
              {t('pages.chat.thread.end')}
            </button>
          )}
          {onEnd && threadSlot && !closedAt && !endError && (
            /* "End" reads final, and a reader who cannot tell whether it can be
               undone does not press it. What ending does -- the thread stays
               readable, the message can carry a new one -- decides whether to press
               the button, so it is visible beside it rather than in a hover
               tooltip, which a touch reader never sees at all. */
            <span className="min-w-0 text-[11px] leading-4 text-muted" data-testid="thread-end-hint">
              {t('pages.chat.thread.end_hint')}
            </span>
          )}
          {endError && (
            /* No hand-off: the hand-off navigates to the main chat, which unmounts
               this panel and discards the draft in the thread's own composer -- and
               a refused end leaves the thread exactly where the reader left it, so
               there is nothing the main chat could add. */
            <ErrorNotice className="flex-1" message={t(endError)} testId="thread-end-error" />
          )}
        </div>
      )}

      {/* The anchor, quoted once at the top. It is the parent conversation's
          row, not one of the thread's messages, so it sits ABOVE the thread's
          own surface rather than inside its transcript. */}
      {/* `max-h` + `overflow-y-auto` and NOT `shrink-0` alone: this region holds
          content of unbounded length -- a quoted message, and a live mirror that
          grows for as long as the parent's turn runs -- so without a cap it takes
          the whole panel and the thread's own surface below gets nothing. The cap
          is a fraction of the panel rather than a pixel figure so it holds at any
          drawer height, and the overflow is on THIS box so a long anchor scrolls
          inside its own region instead of pushing the composer off screen. */}
      <div
        className="shrink-0 flex flex-col max-h-[45%] overflow-y-auto px-3 pt-3 pb-2 border-b border-border"
        data-testid="thread-anchor"
      >
        {detail.isError && (
          /* The anchor could not be read. The thread's own surface below is a
             separate slot and keeps working, so this says only what is missing
             and offers the read again in place. No hand-off: the thread's
             composer below holds an unsaved draft. */
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
          /* Where this quote came from. Without it the reader meets their own
             message twice -- once in the chat behind the drawer, once here -- with
             nothing on screen saying the two are the same row rather than a
             duplicate the thread created. */
          <p className="text-[11px] text-muted m-0 mb-1.5" data-testid="thread-anchor-origin">
            {t('pages.chat.thread.from_main_chat')}
          </p>
        )}
        {parent && (
          <div
            className={anchorExpanded ? undefined : 'max-h-[5.5rem] overflow-hidden'}
            data-testid="thread-anchor-quote"
            data-expanded={anchorExpanded ? 'true' : 'false'}
          >
            {parentIsUser ? (
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
            )}
          </div>
        )}
        {parent && (
          /* Always offered, never conditioned on a measured overflow: the height a
             clamped box WOULD have is a layout read, and gating the only way back
             to the full message on one means a message that measures short by a
             pixel silently loses its control. A short anchor's toggle is a no-op
             the reader can see through; a long anchor's missing toggle is text
             they cannot reach. */
          <button
            type="button"
            data-testid="thread-anchor-toggle"
            aria-expanded={anchorExpanded}
            onClick={() => setAnchorExpanded((on) => !on)}
            className="mt-1 text-[11px] text-accent underline bg-transparent border-none p-0 cursor-pointer hover:text-accent-hover"
          >
            {anchorExpanded ? t('appSdk.chatMessageList.show_less') : t('appSdk.chatMessageList.show_more')}
          </button>
        )}
        {parentLive && (
          /* The parent's turn is STILL RUNNING, so the thread shows it arriving.
             A thread opened on a reply mid-flight otherwise quotes a sentence and
             a half and leaves the reader to switch back to the main chat to see
             how it ended -- while the thing they opened the thread to discuss is
             still being written.

             Read-only, and read-only in the strong sense: the text is the PARENT
             slot's own streaming rows off the store, the parent's turn keeps
             running in the parent's process, and nothing here can steer, stop or
             queue against it. It disappears when that turn ends, because the row
             it mirrors becomes an ordinary assistant row the parent quote above
             already covers. */
          <div
            className={leadWithLive ? 'order-first mb-3' : 'mt-2'}
            data-testid="thread-parent-live"
            data-lead={leadWithLive ? 'true' : 'false'}
          >
            <p className="text-[11px] text-muted m-0 mb-1" data-testid="thread-parent-live-label">
              {t('pages.chat.thread.parent_streaming')}
            </p>
            {/* Its OWN scroll box, capped: this is the one piece of the anchor that
                grows while the reader is looking at it, so an uncapped mirror
                re-creates the defect the region's cap fixes -- every delta pushing
                the thread's surface smaller for as long as the parent keeps
                writing. Bounded here, the arriving reply stays readable and the
                thread stays usable while it arrives. */}
            <div className="flex gap-2 max-h-40 overflow-y-auto" data-testid="thread-parent-live-scroll">
              <div className="shrink-0" style={{ width: AVATAR_PX }} />
              <div className="min-w-0 flex-1 flex flex-col items-start">
                <Bubble side="left" testId="thread-parent-live-bubble">
                  <MessageErrorBoundary rawContent={parentLive}>
                    <MarkdownRenderer content={parentLive} softBreaks streaming />
                  </MessageErrorBoundary>
                </Bubble>
              </div>
            </div>
          </div>
        )}
      </div>

      {legacyReplies.length > 0 && (
        /* A version 1 thread's replies: what they were, read-only. They are not
           replayed into a session's transcript, because that session never had
           them as its history -- no memory injection, read-only tools, a curated
           envelope. Saying so once is cheaper and honest (NOTES D3). */
        <div className={`min-h-0 px-3 py-2 ${threadSlot ? 'shrink-0 max-h-48 overflow-y-auto border-b border-border' : 'flex-1 overflow-y-auto'}`} data-testid="thread-legacy">
          <div className="flex items-center gap-2 mb-1 text-[11px] text-muted">
            <span className="shrink-0 font-semibold">{t('pages.chat.thread.legacy_title')}</span>
            <span className="shrink-0">{t('pages.chat.thread.replies_count', { count: legacyReplies.length })}</span>
            <span className="flex-1 h-px bg-border" aria-hidden="true" />
          </div>
          <p className="text-[11px] text-muted m-0 mb-1" data-testid="thread-legacy-notice">
            {t('pages.chat.thread.legacy_notice')}
          </p>
          <ul className="list-none m-0 p-0">
            {legacyReplies.map((r, i) => (
              <LegacyReplyRow key={r.id} reply={r} pos={legacyPositions[i] ?? 'single'} crewmateName={crewmateName} crewmateLabel={crewmateLabel} youLabel={youLabel} />
            ))}
          </ul>
          {!threadSlot && onStartNew && (
            <div className="mt-3">
              <Btn disabled={startingNew} onClick={onStartNew} data-testid="thread-start-new">
                {t('pages.chat.thread.legacy_start_new')}
              </Btn>
            </div>
          )}
        </div>
      )}

      {threadSlot ? (
        /* The thread itself: the ORDINARY chat pane on the thread's own slot.
           Framed by this panel, so the pane draws no title bar of its own.
           `busyMode` is left at its default split (Steer / Queue): a thread is
           an ordinary session, and the host does not know it to be a DM with
           one named peer. */
        <div className="flex-1 min-h-0 flex flex-col" data-testid="thread-surface">
          <ChatPane
            slotKey={threadSlot}
            frameless
            followContentWidth
            crewmate={crewmate}
            /* An ENDED thread keeps its composer, because ending releases the
               anchor and not the session -- the same session is still writable
               from its own full page, so disabling it only here would be a second
               rule. The hint says so instead, where the reader is about to type. */
            threadSurface={closedAt ? 'closed' : 'open'}
            onOpenFull={onOpenFull ? (s: string) => onOpenFull(s) : undefined}
          />
        </div>
      ) : legacyReplies.length === 0 ? (
        <div className="flex-1 min-h-0 flex items-center justify-center px-3 text-center text-[12px] text-muted" data-testid="thread-empty">
          {detail.isLoading ? t('pages.chat.thread.opening') : t('pages.chat.thread.no_replies_yet')}
          {!detail.isLoading && onStartNew && (
            <span className="ml-2">
              <Btn disabled={startingNew} onClick={onStartNew} data-testid="thread-start-new">
                {t('pages.chat.thread.legacy_start_new')}
              </Btn>
            </span>
          )}
        </div>
      ) : null}
    </div>
  )
}
