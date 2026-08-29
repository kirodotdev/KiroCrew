import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { useAppDispatch } from '../store'
import { cancelQueuedMessage, applyQueueEdit, rollbackQueueEdit } from '../store/chatSlice'
import { restoreQueuedContent } from '../utils/fileTokens'
import { mintSendId } from '../pages/chat/ChatPageMessageContent'
import type { ChatMessage } from '../types'
import { rollbackLocalQueueEdit, queuedSendStash } from '../utils/queuedSendStash'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

interface EditVars { slot: string; queueId: string; content: string; previous: string; editId: string }


export {
  queuedSendStash,
  preSendStash,
  stashPreSend,
  retirePreSendStash,
  adoptPreSendStash,
  stashQueuedSend,
  type QueuedSendRecord,
} from '../utils/queuedSendStash'

/** The four queue-card callbacks `QueueStack` takes, plus the in-flight set it
 *  disables its controls from. */
export interface QueuedMessageActions {
  /** A rejected queue edit, for the host to render. Null when there is nothing to report. */
  editError: string | null
  dismissEditError: () => void
  onCancel: (queueId: string) => void
  onInterrupt: (queueId: string) => void
  onEdit: (queueId: string, content: string) => void
  onReorder: (queueId: string, direction: 'next' | 'later') => void
  /** Feed straight to `QueueStack`'s `pendingIds`. */
  pendingIds: ReadonlySet<string>
}

export interface QueuedMessageActionsOptions {
  /** Slot the cards belong to. Null/empty disables every action, which is what
   *  a host with no active slot needs. */
  slot: string | null | undefined
  /** EVERY `role: 'queued'` row in the slot, including the ones no card is drawn
   *  for (hidden system deliveries, recovery continuations). Reorder submits the
   *  full sequence, so omitting them would let the backend re-append them at the
   *  tail and silently demote automation. */
  allQueued: ChatMessage[]
  /** Just the interactive cards `QueueStack` renders, in render order. A reorder
   *  swaps two ADJACENT VISIBLE cards, so the neighbour is chosen here and then
   *  expressed inside `allQueued`'s full sequence. */
  visibleQueued: ChatMessage[]
  /** Hand a cancelled card's recovered composer state back to the host. Each
   *  host owns its own composer — ChatPage's `input` is draft-persisted per
   *  slot, a pane's is local state that merges a recovered draft — so the
   *  plumbing is injected rather than decided here. `text` is the TYPED text
   *  (from the send-time stash when this tab made the send, else inverted
   *  from the wire serialization by `restoreQueuedContent`), never the raw
   *  card content — the card holds `prepareSendPayload`'s LLM-facing form
   *  (`[attached_file N]` markers, image markdown), and restoring it verbatim
   *  is the marker-in-composer data loss of #560. `files` are the attachment
   *  paths to re-stage; a host MUST merge them into its pending-files state
   *  or the recovered text refers to attachments the re-send will not carry.
   *  Omitted, the recovered state is dropped, which is what cancelling in a
   *  split pane did before #5891 and what no host should do.
   */
  restoreDraft?: (text: string, files: string[]) => void
}

const queueIdOf = (m: ChatMessage): string | undefined => m.meta?.queueId as string | undefined

/**
 * The queue-card action recipe, owned once and consumed by every host that draws
 * a `QueueStack` over the MAIN slot queue (`ChatPage` and `ChatPane` today).
 *
 * Issue #5891: the four callbacks were copy-mirrored across those hosts, and the
 * copies had drifted — cancel restored the composer in `ChatPage` only, so
 * cancelling from a split pane silently destroyed the draft, and neither host
 * threaded the `pendingIds` latch `QueueStack` has always accepted. Both are
 * behaviours per surface, which is exactly why they must not live in the hosts:
 * fixing one fork does not fix the other, and #2240 was that failure mode.
 *
 * NOT a home for `SideChat`'s queue. That surface talks to a different endpoint
 * family (`/side/queue/…`), stores its cards as `SideQueueEntry[]` under
 * `slotSide[slot]` rather than as `role: 'queued'` transcript rows, and is
 * deliberately SERVER-AUTHORITATIVE where this one is optimistic — the inverse
 * contract, for a documented reason. See that file, and the PR for #5891.
 *
 * Cancel and edit stay optimistic and still swallow a failed mutation, matching
 * what both hosts did before this hook existed. Item 1 of #5891 — rollback or
 * dispatch-after-success, plus an error surface — is a contract change with two
 * viable answers, and lands here, once, when someone picks one.
 */
export function useQueuedMessageActions({
  slot,
  allQueued,
  visibleQueued,
  restoreDraft,
}: QueuedMessageActionsOptions): QueuedMessageActions {
  const dispatch = useAppDispatch()
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(() => new Set())

  // Reads happen inside callbacks that must NOT be re-created when the queue
  // changes: `QueueStack` is memo-compared on callback identity, so a new
  // `onCancel` on every queue mutation would repaint the whole stack mid-animation.
  const allQueuedRef = useRef(allQueued)
  allQueuedRef.current = allQueued
  const visibleQueuedRef = useRef(visibleQueued)
  visibleQueuedRef.current = visibleQueued
  const restoreDraftRef = useRef(restoreDraft)
  restoreDraftRef.current = restoreDraft

  const markPending = useCallback((queueId: string, pending: boolean) => {
    setPendingIds(prev => {
      if (pending === prev.has(queueId)) return prev
      const next = new Set(prev)
      if (pending) next.add(queueId)
      else next.delete(queueId)
      return next
    })
  }, [])

  // Ids whose interrupt the server ACCEPTED and whose card has not yet been
  // retired. The HTTP 200 is not the end of an interrupt: the entry is dequeued
  // and started, and the card only goes away when the `queue_pop` frame lands.
  // Releasing on the response would re-enable the button inside that gap, and
  // the next click would interrupt the very turn the first click just promoted.
  // So a successful interrupt stays latched until its row is gone.
  const [heldUntilRetired, setHeldUntilRetired] = useState<ReadonlySet<string>>(() => new Set())
  const queuedIdSet = useMemo(
    () => new Set(allQueued.map(queueIdOf).filter((id): id is string => !!id)),
    [allQueued],
  )
  useEffect(() => {
    // Residual, deliberately: if that frame never arrives the control stays
    // disabled. That is the safe direction - it withholds a click that would
    // interrupt a turn the user did not aim at - and it clears on the next
    // hydration of the slot, which rebuilds the queued rows from the server.
    const gone = [...heldUntilRetired].filter(id => !queuedIdSet.has(id))
    if (!gone.length) return
    setHeldUntilRetired(prev => {
      const next = new Set(prev)
      for (const id of gone) next.delete(id)
      return next
    })
    setPendingIds(prev => {
      if (!gone.some(id => prev.has(id))) return prev
      const next = new Set(prev)
      for (const id of gone) next.delete(id)
      return next
    })
  }, [queuedIdSet, heldUntilRetired])

  // No mounted-ref guard around the releases below, deliberately. React 18.3
  // dropped the setState-after-unmount warning, so a request that outlives its
  // host (closing a split pane mid-flight) costs one dropped state write and
  // nothing else — while the obvious guard is actively wrong here: this app
  // renders under StrictMode, whose mount/unmount/remount of effects would
  // latch a `mounted` ref to false for the rest of the session and leave every
  // card's controls disabled after their first click. The StrictMode test in
  // useQueuedMessageActions.test.tsx pins that release survives the remount.
  /** Latch the card for the life of the request so a second click cannot fire a
   *  duplicate, releasing on either outcome. Used by the two actions whose
   *  optimistic dispatch settles the card itself: cancel retires it and edit
   *  rewrites it, so the response is the whole story and there is nothing left
   *  to wait for.
   *
   *  The failure path stays silent for CANCEL, matching what both hosts did before
   *  this hook existed. The EDIT path no longer does: #5891 item 1 is RESOLVED here,
   *  by the rollback and the inline error below. It must still
   *  release, or one lost request freezes a card until the queue drains. */
  const run = useCallback((queueId: string, call: Promise<unknown>) => {
    markPending(queueId, true)
    const release = () => markPending(queueId, false)
    call.then(release, release)
  }, [markPending])

  const onCancel = useCallback((queueId: string) => {
    if (!slot) return
    const msg = allQueuedRef.current.find(m => queueIdOf(m) === queueId)
    if (msg?.content) {
      // Restore by QUEUE IDENTITY: the record was stored under the queue id
      // the send receipt returned, which is the id this cancel carries — so a
      // hit is this card's own pre-send state by construction, whatever its
      // content collides with (duplicate texts, erased-image-token captions,
      // other tabs). The record is consumed either way; `sent` guards the one
      // same-id hazard (see QueuedSendRecord). No stash hit — a reload,
      // another tab's card, an edited entry — falls to the strict parser,
      // which claims only byte-exact round-trippable shapes and is never
      // worse than the verbatim restore this replaced.
      const stashed = queuedSendStash.get(queueId)
      if (stashed) queuedSendStash.delete(queueId)
      // Second source for the same fact: when the receipt was unreadable no stash
      // was written, and the card's own text is the redacted form (#6825).
      const carried = msg.meta?.rawSend as { text?: string; files?: string[]; sent?: string } | undefined
      // Not content alone, and not the ROW's flag either: a remote frame clears that while THIS tab's
      // edit is still outstanding. A rolled-back edit is exempt -- it changed nothing server-side.
      const editUnresolved = typeof stashed?.pendingEdit === 'string' && stashed.editRolledBack !== true
      const fromStash = stashed && stashed.sent === msg.content
        && !msg.meta?.editPending && !editUnresolved
      const { text, files } = fromStash
        ? { text: stashed.raw, files: stashed.files }
        : carried && typeof carried.text === 'string' && carried.sent === msg.content
          ? { text: carried.text, files: carried.files || [] }
          : restoreQueuedContent(msg.content)
      restoreDraftRef.current?.(text, files)
    }
    // Optimistically remove the card; the WS echo is a no-op if already gone.
    dispatch(cancelQueuedMessage({ slot, queue_id: queueId }))
    run(queueId, api.cancelQueuedMessage(slot, queueId))
  }, [slot, dispatch, run])

  const onInterrupt = useCallback((queueId: string) => {
    if (!slot) return
    markPending(queueId, true)
    api.interruptSlot(slot, queueId).then(
      // Accepted: the entry is being promoted, so stay latched until the row is
      // gone rather than until this response landed.
      () => setHeldUntilRetired(prev => (prev.has(queueId) ? prev : new Set(prev).add(queueId))),
      // Rejected: nothing was promoted and the card is still the same card, so
      // release at once and let the user try again.
      () => markPending(queueId, false),
    )
  }, [slot, markPending])

  /** Confirmed only by the SERVER taking it: nothing here rolls the optimistic edit back, so a
   *  refetch restores the original entry and the stash is what a cancel then recovers. */
  const [editError, setEditError] = useState<string | null>(null)
  const dismissEditError = useCallback(() => setEditError(null), [])

  const editMutation = useMutation({
    mutationFn: ({ slot: s, queueId, content, editId }: EditVars) =>
      api.editQueuedMessage(s, queueId, content, editId),
    onMutate: ({ queueId }) => { setEditError(null); markPending(queueId, true) },
    // The SERVER's text, not the request's: an edit that drops an attachment marker is
    // renumbered server-side, so replaying the submitted text diverges the card from the queue.
    // `expect` is the optimistic text this response answers, and `editRev` ORDERS it against what
    // has been applied, which is what tells a superseded edit from an echo that arrived first.
    onSuccess: (data, { slot: s, queueId, content }) => {
      const d = data as { content?: unknown; editRev?: unknown } | undefined
      dispatch(applyQueueEdit({
        slot: s, queue_id: queueId, confirmed: true, expect: content,
        content: typeof d?.content === 'string' && d.content ? d.content : content,
        ...(typeof d?.editRev === 'number' ? { editRev: d.editRev } : {}),
      }))
    },
    // Conditional: the commit is broadcast on a separate channel, so a lost response can land after
    // the echo confirmed this edit -- undoing it then shows text the agent is not running.
    onError: (err, { slot: s, queueId, content, previous }) => {
      // The row's `editPending` is cleared by the reducer; the STASH needs telling too, or a later
      // remote edit finds a record it is not allowed to retire and a cancel restores pre-edit text.
      rollbackLocalQueueEdit(queueId)
      dispatch(rollbackQueueEdit({ slot: s, queue_id: queueId, previous, optimistic: content }))
      setEditError(i18nT('components.queueStack.edit_failed', {
        error: errMessage(err) || i18nT('components.errorBoundary.something_went_wrong'),
      }) as string)
    },
    onSettled: (_data, _err, { queueId }) => markPending(queueId, false),
  })
  const editEntry = editMutation.mutate

  const onEdit = useCallback((queueId: string, content: string) => {
    if (!slot) return
    const trimmed = content.trim()
    if (!trimmed) return
    // Read BEFORE the optimistic dispatch: after it the store already holds the new text, so the
    // rollback basis has to be carried through the mutation rather than re-read in a handler.
    const previous = allQueuedRef.current.find(m => queueIdOf(m) === queueId)?.content ?? ''
    // Minted here so the optimistic dispatch and the request name the SAME edit: the echo carries it
    // back, and that is the only thing separating this tab's own frame from a concurrent editor's.
    const editId = mintSendId()
    dispatch(applyQueueEdit({ slot, queue_id: queueId, content: trimmed, confirmed: false, editId }))
    editEntry({ slot, queueId, content: trimmed, previous, editId })
  }, [slot, dispatch, editEntry])

  const onReorder = useCallback((queueId: string, direction: 'next' | 'later') => {
    if (!slot) return
    const fullIds = allQueuedRef.current.map(queueIdOf).filter((id): id is string => !!id)
    const visibleIds = visibleQueuedRef.current.map(queueIdOf).filter((id): id is string => !!id)
    const vFrom = visibleIds.indexOf(queueId)
    const vTo = direction === 'next' ? vFrom - 1 : vFrom + 1
    if (vFrom < 0 || vTo < 0 || vTo >= visibleIds.length) return
    const a = fullIds.indexOf(visibleIds[vFrom])
    const b = fullIds.indexOf(visibleIds[vTo])
    if (a < 0 || b < 0) return
    const next = [...fullIds]
    ;[next[a], next[b]] = [next[b], next[a]]
    // No optimistic dispatch: the server commits and broadcasts queue_reorder to
    // every client including this one, and that WS event is the authoritative
    // store update. A local dispatch with rollback-on-failure could restore a
    // stale order when the server committed but the HTTP response was lost,
    // leaving this client in conflict with execution order.
    //
    // Unlatched for the same reason the arrows are not gated on `pendingIds`
    // inside QueueStack: with no optimistic move, a latch would freeze the arrows
    // on a card that has not visibly moved yet, and the repeat it would block is
    // an identical full-order PUT the server can absorb.
    api.reorderQueuedMessages(slot, next).catch(() => undefined)
  }, [slot])

  return useMemo(
    () => ({ onCancel, onInterrupt, onEdit, onReorder, pendingIds, editError, dismissEditError }),
    [onCancel, onInterrupt, onEdit, onReorder, pendingIds, editError, dismissEditError],
  )
}
