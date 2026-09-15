import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { useAppDispatch } from '../store'
import { cancelQueuedMessage, editQueuedMessage } from '../store/chatSlice'
import { restoreQueuedContent } from '../utils/fileTokens'
import type { ChatMessage } from '../types'

/** Pre-serialization composer state of a send the server QUEUED, written by the
 *  host's send path when the `queued: true` receipt names the entry. */
export interface QueuedSendRecord {
  /** The text exactly as the user typed it. */
  raw: string
  /** The staged file paths at send time. */
  files: string[]
  /** The exact POSTed LLM-facing text — the edit guard: an entry edited after
   *  send keeps its queue id but fails this equality, so an edited card falls
   *  to the parser instead of clobbering the edit with pre-edit state. */
  sent: string
}

/** Queued-send stash, keyed by the `queue_id` the send receipt returns (the
 *  same id `queue_push` broadcasts and the card's cancel button carries).
 *  Queue identity is the ONLY sound key: the serialization is not injective
 *  (image @-tokens are erased from the LLM-facing text), so content-keyed
 *  records can collide across different captions, duplicate sends, and other
 *  tabs. Module-level so every host's send path (ChatPage, ChatPane) writes
 *  the one store this hook's cancel consumes — the same one-owner reasoning
 *  as the hook itself (#5891). Deliberately unevicted: an entry dies on the
 *  cancel that consumes it, and evicting a live entry would degrade that
 *  card's cancel to the parser fallback; orphans from normal delivery are
 *  three small strings bounded by queued sends per tab session. */
export const queuedSendStash = new Map<string, QueuedSendRecord>()

/** The four queue-card callbacks `QueueStack` takes, plus the in-flight set it
 *  disables its controls from. */
export interface QueuedMessageActions {
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
   *  The failure path stays silent, matching what both hosts did before this
   *  hook existed and leaving item 1 of #5891 to decide otherwise. It must still
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
      const { text, files } = stashed && stashed.sent === msg.content
        ? { text: stashed.raw, files: stashed.files }
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

  const onEdit = useCallback((queueId: string, content: string) => {
    if (!slot) return
    const trimmed = content.trim()
    if (!trimmed) return
    // Optimistically update the card; the WS event reconciles other clients.
    dispatch(editQueuedMessage({ slot, queue_id: queueId, content: trimmed }))
    run(queueId, api.editQueuedMessage(slot, queueId, trimmed))
  }, [slot, dispatch, run])

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
    () => ({ onCancel, onInterrupt, onEdit, onReorder, pendingIds }),
    [onCancel, onInterrupt, onEdit, onReorder, pendingIds],
  )
}
