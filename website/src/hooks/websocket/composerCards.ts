/** The cards the socket places above a slot's composer: question cards
 *  (blocking asks and stateless cards), follow-up suggestions and folder
 *  suggestions, plus the reconnect reconcile that keeps question cards true to
 *  the server's pending set. */
import { useMemo, useRef } from 'react'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { setQuestionCard, resolveQuestionCard, setFollowupCard, setFolderSuggestion } from '../../store/chatSlice'
import { dispatchMcNotification, APPROVAL_KIND } from '../notificationEvent'
import { loadUnreadOnAttention } from '../unreadOnAttention'
import { api } from '../../api/client'
import { isSlotOnScreen } from './attention'
import { recordInBoundedLog, resolvedSince } from './retiredIds'
import type { FrameData } from './frames'

/** The server-side IDENTITY of a held card: a blocking ask's `ask_id`, or a
 *  stateless card's server-minted `card_id`. Both kinds are listed by
 *  `GET /api/ask-question/pending`, so both can be reconciled against it.
 *
 *  A card with neither (an entry built by a fixture, or delivered before the
 *  server kept a record) has no identity to compare, and absence from the
 *  snapshot says nothing about it — those are skipped rather than reported
 *  stale. */
export function identityOf(card: { ask_id?: string; serverCardId?: string } | undefined): string {
  return card?.ask_id || card?.serverCardId || ''
}

/** Own-property card identities held in the pending-question map, in map order. */
export function askIdsOf(
  map: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
): string[] {
  return Object.values(map ?? {})
    .map((card) => identityOf(card))
    .filter((id): id is string => !!id)
}

/** Decide what a reconnect reconcile should drop and re-add.
 *
 *  Pure so the race can be tested directly. `before` is the pending-question map
 *  captured BEFORE the HTTP snapshot was requested and `after` the map as it
 *  stands once the response arrives; the difference is exactly what live WS
 *  events did while the request was in flight.
 *
 *  - Only ids present in `before` may be dropped. A `question_card` that arrived
 *    during the fetch is absent from the response, and deleting it would leave
 *    the agent blocked until its timeout.
 *  - An id that vanished locally during the fetch was resolved by a WS event, so
 *    the response's copy is already dead and must not be re-added — a
 *    resurrected card can only 404 on submit.
 *
 *  Both kinds of card go through this, keyed by `identityOf`: the server records
 *  and lists stateless cards too, so a tab that was disconnected while its card
 *  was retired or replaced must have it removed, not merely be denied a
 *  duplicate. A card whose identity is absent from the snapshot is stale in
 *  exactly the same sense for both kinds.
 */
export function reconcileQuestions<
  T extends { ask_id?: string; card_id?: string; slot?: string; questions?: unknown[] },
>(
  before: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  after: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  pending: T[],
  resolvedDuringFetch: string[] = [],
): { drop: string[]; add: T[] } {
  const afterIds = new Set(askIdsOf(after))
  // Two independent sources, because neither alone is sufficient:
  //  - the before/after diff catches a card that WAS local and disappeared
  //    (including one this client resolved itself, with no WS event involved);
  //  - the observed resolution log catches an identity this client never held, so
  //    there was nothing for the diff to notice. That is the case where the
  //    snapshot alone would resurrect a dead card.
  const dead = new Set([
    ...askIdsOf(before).filter((id) => !afterIds.has(id)),
    ...resolvedDuringFetch,
  ])
  const beforeIds = new Set(askIdsOf(before))
  /** True when *slot* now holds a DIFFERENT card that the snapshot cannot know
   *  about — one that arrived while the request was in flight (its identity is
   *  absent from `before`). The same ordering argument as the drop side, applied
   *  to adds: one card renders per slot, so adding the snapshot's row would
   *  replace a newer live card with a stale one. */
  const arrivedDuringFetch = (slot: string | undefined, identity: string): boolean => {
    if (!slot) return false
    const held = identityOf(after?.[slot])
    return !!held && held !== identity && !beforeIds.has(held)
  }
  return {
    drop: staleAskIds(before, pending),
    add: pending.filter((q) => {
      const identity = q.ask_id || q.card_id || ''
      return (
        !!q.slot &&
        !!q.questions?.length &&
        !dead.has(identity) &&
        !arrivedDuringFetch(q.slot, identity)
      )
    }),
  }
}

/** Card identities held locally that the server no longer lists as pending.
 *
 *  `question_card` and `question_card_resolved` are one-shot broadcasts, so a
 *  reload or reconnect can miss either one: a card that should be showing is
 *  absent, or one retired while disconnected is still on screen. Reconnect
 *  therefore reconciles in both directions rather than only adding — for a
 *  stateless card as much as a blocking one, since keeping a retired or
 *  superseded card would send its answer against a question the agent has
 *  already moved past.
 *
 *  A card with no identity at all is never reported stale: there is nothing to
 *  compare, so its absence from the response says nothing about it.
 *  Exported so this is unit-testable without standing up a live socket.
 */
export function staleAskIds(
  current: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  pending: { ask_id?: string; card_id?: string }[],
): string[] {
  const live = new Set(pending.map((q) => q.ask_id || q.card_id || '').filter(Boolean))
  return askIdsOf(current).filter((id) => !live.has(id))
}

export interface ComposerCards {
  /** Reconcile question cards against the server's pending set (see below). */
  syncPendingQuestions(): Promise<void>
  onQuestionCard(data: FrameData, reconnecting: boolean): void
  onQuestionCardResolved(data: FrameData): void
  onFollowupCard(data: FrameData): void
  onFolderSuggestion(data: FrameData): void
}

export function useComposerCards(dispatch: AppDispatch): ComposerCards {
  /* Resolutions observed on the wire, ask_id -> monotonic sequence. Needed
     because a `question_card_resolved` can name a card this client never held
     (it exists only inside an in-flight rehydration snapshot), so the dispatch
     is a no-op and local state carries no trace of it. Bounded, and keyed by id
     with a sequence so trimming never shifts a watermark's meaning. */
  const resolvedAskIdsRef = useRef<Map<string, number>>(new Map())
  const resolvedSeqRef = useRef(0)

  return useMemo<ComposerCards>(() => ({
    /** `question_card` and `question_card_resolved` are one-shot broadcasts, so a
     *  reload or reconnect can miss either one: a card that should be showing is
     *  absent, or one resolved while we were disconnected is still on screen.
     *  This is a two-way reconcile rather than an add-only sync for that reason.
     *
     *  The snapshot is taken BEFORE the fetch, and that ordering is the whole
     *  correctness argument. The HTTP response describes the server as it was when
     *  the request was served, so it races live WS events both ways:
     *   - a `question_card` arriving DURING the fetch is absent from the response;
     *     reconciling against post-fetch state would delete it and leave the agent
     *     blocked until timeout. Only ids present before the fetch can be dropped,
     *     and Redux state is immutable, so the pre-fetch reference cannot contain
     *     a later addition.
     *   - a `question_card_resolved` arriving during the fetch leaves a card in the
     *     response that is already dead; re-adding it would resurrect a card whose
     *     submit can only 404. Those ids are skipped on the add side.
     *  Legacy cards (no ask_id) are preserved -- the server has no record of them,
     *  so their absence from the response is not evidence they are stale. */
    async syncPendingQuestions() {
      try {
        const before = store.getState().chat.pendingQuestions
        // Watermark the resolution log before the request so the ids that arrive
        // while it is in flight can be identified afterwards. This is the only
        // signal that covers a resolution for a card this client never held —
        // `before`/`after` cannot see it, because there was nothing to remove.
        const resolvedSeen = resolvedSeqRef.current
        const pending = await api.pendingQuestions()
        // ONE reconcile for both kinds. The server records and lists stateless
        // cards, so their absence from the snapshot is evidence in the same way a
        // blocking ask's is: a tab that was disconnected while its card was retired
        // or superseded must lose it, or submitting it answers a question the agent
        // has already moved past.
        const { drop, add } = reconcileQuestions(
          before,
          store.getState().chat.pendingQuestions,
          pending,
          resolvedSince(resolvedAskIdsRef.current, resolvedSeen),
        )
        // Identity-keyed retirement: an entry is dropped by whichever id it holds.
        const heldBefore = Object.values(before ?? {})
        for (const id of drop) {
          const wasBlocking = heldBefore.some((c) => c?.ask_id === id)
          dispatch(resolveQuestionCard(wasBlocking ? { ask_id: id } : { card_id: id }))
        }
        for (const q of add) {
          // A stateless row carries `card_id`; a blocking one carries `ask_id`. The
          // reducer coalesces a structurally identical re-delivery, so re-adding a
          // card this tab already holds keeps the mounted component and the user's
          // half-entered answer rather than churning it.
          dispatch(setQuestionCard({
            slot: q.slot as string,
            ask_id: q.ask_id,
            card_id: q.card_id,
            native: q.native,
            questions: q.questions as Parameters<typeof setQuestionCard>[0]['questions'],
          }))
        }
      } catch { /* ignore */ }
    },
    onQuestionCard(data, reconnecting) {
      const previous = store.getState().chat.pendingQuestions?.[data.slot]
      // Every card carries its server identity (`ask_id` or `card_id`);
      // the reducer coalesces a re-delivery of the same id. Audio
      // deduplicates by that id too.
      dispatch(setQuestionCard(data as Parameters<typeof setQuestionCard>[0]))
      const current = store.getState().chat.pendingQuestions?.[data.slot]
      const id = data.ask_id || data.card_id
      if (current?.slot === data.slot && !reconnecting
          && (!id || identityOf(previous) !== id)) {
        dispatchMcNotification(APPROVAL_KIND)
        // A new card means the agent waits on the user. The default mode
        // already badged the session on the rows that led here.
        if (loadUnreadOnAttention() && !isSlotOnScreen(data.slot)) dispatch(markSlotUnread({ slot: data.slot }))
      }
    },
    onQuestionCardResolved(data) {
      const ask = data as { ask_id?: string; card_id?: string }
      // Recorded independently of local state: a resolution can arrive for
      // a card this client never held (empty state, or the card only exists
      // in an in-flight rehydration snapshot), in which case the dispatch
      // below is a no-op and the reconcile would otherwise re-add a dead
      // card. Both identities land in the same log — a blocking ask's
      // `ask_id` and a stateless card's `card_id` — so one watermark covers
      // both kinds on the snapshot add side.
      recordInBoundedLog(resolvedAskIdsRef.current, resolvedSeqRef, ask.ask_id || ask.card_id || '')
      dispatch(resolveQuestionCard(ask))
    },
    onFollowupCard(data) {
      // Agent-authored follow-up suggestions. The server caps this at 3
      // items and has already sanitized + redacted every string; the
      // slice keeps only the fields the card renders.
      const raw = data as { slot?: string; items?: Array<Record<string, unknown>>; ts?: number }
      const items = (Array.isArray(raw.items) ? raw.items : [])
        .filter((it) => it && typeof it.title === 'string' && typeof it.prompt === 'string')
        .map((it) => ({
          title: String(it.title),
          description: typeof it.description === 'string' ? it.description : '',
          prompt: String(it.prompt),
          ...(typeof it.branch === 'string' && it.branch ? { branch: it.branch } : {}),
        }))
      if (raw.slot && items.length) {
        dispatch(setFollowupCard({
          slot: raw.slot,
          items,
          ...(typeof raw.ts === 'number' ? { ts: raw.ts } : {}),
        }))
      }
    },
    onFolderSuggestion(data) {
      // Post-titling offer to file an unfiled session. Every field is the
      // user's own stored folder data (the backend model call returns an
      // index, not text), but the shape is still validated here so a
      // malformed frame cannot render an empty or half-filled card.
      const raw = data as { slot?: string; folder_id?: string; folder_name?: string; breadcrumb?: string; ts?: number }
      if (raw.slot && typeof raw.folder_id === 'string' && raw.folder_id && typeof raw.folder_name === 'string' && raw.folder_name) {
        dispatch(setFolderSuggestion({
          slot: raw.slot,
          folderId: raw.folder_id,
          folderName: raw.folder_name,
          breadcrumb: typeof raw.breadcrumb === 'string' ? raw.breadcrumb : '',
          ...(typeof raw.ts === 'number' ? { ts: raw.ts } : {}),
        }))
      }
    },
  }), [dispatch])
}
