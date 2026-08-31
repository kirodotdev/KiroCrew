import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MessageCircleQuestionMark, RotateCcw } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAppSelector, useAppDispatch } from '../../store'
import { sideClose, sideOptimisticAppend, sideOptimisticRollback, sseSideQueue, sideReleaseConsumed, queueEditBroadcastAt } from '../../store/chatSlice'
import QueueStack from '../../components/QueueStack'
import { useChatScrollFollow } from '../../app-sdk/useChatScrollFollow'
import ChatMessageList from '../../app-sdk/ChatMessageList'
import FollowUpBar from '../../components/FollowUpBar'
import { deriveFollowUpOptions } from '../../app-sdk/protocol'
import { useComposerDraft, draftByteSize } from '../../app-sdk/useComposerDraft'
import ChatInput from '../../components/ChatInput'
import { SlotProvider } from '../../providers/SlotContext'
import { useConnected } from '../../hooks/useConnected'
import type { SideMessage, SideQueueEntry } from '../../store/chatSlice'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
const MAX_QUESTION_BYTES = 32_768
// Max auto-grow height (px) for the side-question input before it scrolls.
const MAX_INPUT_H = 240
// How long the transient "queued instead" notice stays up. It describes a moment,
// not a state, so leaving it until the next submit would let it sit beside a later
// turn it has nothing to do with.
const NOTICE_TTL_MS = 8_000
// Stable fallbacks for a slot with no side buffer yet. An inline `?? []` allocates
// a fresh array every render, so the transcript map, the queue cards and the
// blocked-id set would all recompute on every render of an EMPTY panel — the one
// case where there is provably nothing to recompute. Never mutated: both are read
// only through `.length`, indexing and `.map`.
const EMPTY_SIDE_MESSAGES: SideMessage[] = []
const EMPTY_SIDE_QUEUE: SideQueueEntry[] = []

/** One composer submit. `steer` and `optimistic` are decided at submit time and
 *  carried along, so a mutation callback that runs later never re-derives them
 *  from state its own optimistic update already changed. */
/** `slot` is captured at submit time: the panel's prop can change under an in-flight
 *  request, and a response must land where the question was asked. */
type SideSubmit = { q: string; steer: boolean; optimistic: boolean; slot: string;
  /** True when `q` came from a follow-up chip rather than the composer, so the draft the
   *  user is still writing must survive the send. */
  override?: boolean }

/** Put `released` text back in the composer without discarding what is there.
 *
 *  Both texts are typed work: choosing either one destroys the other, and the
 *  released text has no other home (its card or its request is already gone), so
 *  it cannot be the one dropped. Appending keeps both and leaves the user to
 *  edit — visible, undoable by hand, and never a silent loss. */
/** Raw submitted texts retained for restore-on-cancel. Only the recent past can
 *  still be cancelled, so a small window is enough. */
/** Namespaces steer-ledger keys in `submittedRaw` so they cannot collide with
 *  queue ids. */
const STEER_RAW_PREFIX = 'steer:'
const MAX_SUBMITTED_RAW = 50

function relativeTime(iso: string): string | null {  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 30 * 60_000) return null
  if (diff < 60 * 60_000) return `${Math.floor(diff / 60_000)}m`
  if (diff < 24 * 3600_000) return `${Math.floor(diff / 3600_000)}h`
  return `${Math.floor(diff / (24 * 3600_000))}d`
}

export default function SideChat({ slot }: { slot: string }) {
  const connected = useConnected()
  const dispatch = useAppDispatch()
  const reduxSide = useAppSelector(s => s.chat.slotSide[slot])
  const parentTurnCount = useAppSelector(s =>
    s.chat.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
  )
  const [localError, setLocalError] = useState<string | null>(null)
  // Transient, non-error feedback (e.g. a steer the server had to demote to a
  // queue entry). Kept apart from localError so it renders as a notice, not red.
  const [localNotice, setLocalNotice] = useState<string | null>(null)

  // Retire the notice on its own so it cannot outlive the moment it describes.
  useEffect(() => {
    if (!localNotice) return
    const t = setTimeout(() => setLocalNotice(null), NOTICE_TTL_MS)
    return () => clearTimeout(t)
  }, [localNotice])
  // Stick-to-bottom follow shared with ChatPane/ChatEmbed (FollowController
  // semantics): the RO on the content wrapper re-pins on growth ANYWHERE in
  // the transcript and on collapse shrink, and only a genuine user scroll up
  // releases it.
  const follow = useChatScrollFollow({ resetKey: slot })
  const scrollRef = follow.scrollerRef

  const messages = reduxSide?.messages ?? EMPTY_SIDE_MESSAGES
  const isPending = reduxSide?.pending ?? false
  const queue = reduxSide?.queue ?? EMPTY_SIDE_QUEUE
  // Steer-vs-queue mode lives inside ChatInput's own split button, keyed by the
  // same slot as the main composer — the side panel and the main chat of ONE
  // session share the mode while other sessions keep their own.
  // A turn is in flight, so a submit can no longer just start one. Derived from
  // the same signal the thinking indicator uses, so the composer's affordance and
  // what the server will actually do can't disagree.
  const isBusy = isPending || (reduxSide?.streaming ?? false)

  const lastIdx = messages.length - 1
  const lastMsg = messages[lastIdx]
  const isStreaming = reduxSide?.streaming ?? false
  const isStreamingLast = lastMsg?.role === 'assistant' && isStreaming

  /** The side buffer carries only `user` / `assistant` plus an `is_error` flag, so the
   *  roles the shared transcript understands are derived here rather than stored. The last
   *  assistant message becomes `streaming` while the turn runs, which is what drives the
   *  cursor and holds the footer back until the answer settles. */
  const transcript = useMemo<ChatMessage[]>(
    () => messages.map((m, i) => {
      const streaming = i === lastIdx && isStreamingLast
      const role = m.role === 'user' ? 'user' : m.is_error ? 'error' : streaming ? 'streaming' : 'assistant'
      return { role, content: m.content, cls: `msg msg-${role}`, ts: m.ts }
    }),
    [messages, lastIdx, isStreamingLast]
  )

  /** Derived from the same helper the main chat uses, so "options only after the answer
   *  settles" and "a later user message clears them" behave identically.
   *
   *  `followUpIsPlan` is DELIBERATELY dropped here (#6754; sibling issue #6057 covers
   *  the same drop in ChatEmbed): this side panel is not a plan-capable host, so a
   *  plan-shaped chip stays on the composer-draft path instead of dispatching
   *  POST /api/chat/slots/{slot}/plan-action. Why that is a recorded exclusion rather
   *  than a live mis-dispatch:
   *  - A side turn runs as an aside to the parent session, never as an orchestrator
   *    turn, so a plan-shaped answer in the side buffer is conversational output, not
   *    a plan awaiting dispatch; `useComposerDraft` owning the chip (pick → edits the
   *    draft, amendable before send) is therefore the correct behaviour, not a
   *    fallback.
   *  - The dispatch path gates on the HOST slot's mode (ChatPage reads
   *    `effectiveMode === 'orchestrator'` off the slot record before dispatching).
   *    This panel does not read the host slot's mode today — its transcript is the
   *    side buffer, a private mini-conversation beside the parent slot — and wiring
   *    `usePlanActionMutation` in would first need that mode selector added, gating
   *    on the PARENT's mode for a conversation that is not the parent's.
   *  - An unconditional dispatch (no mode gate) would let any plan-shaped side
   *    answer cancel or advance the parent's real plan.
   *  Pinned by src/test/SideChat.planExclusion.test.tsx. */
  const { followUpOptions, followUpRecommended } = useMemo(
    () => deriveFollowUpOptions(transcript, isStreaming),
    [transcript, isStreaming]
  )

  /** The composer's draft behaviour, owned by the chat SDK rather than by this file: what a
   *  follow-up pick does to the text, where a handed-back submit goes, when Enter is a send
   *  and when it is an IME committing a candidate, and how tall the box may grow. Derived
   *  here from `followUpOptions`, so the hook has to be called after that. */
  const composer = useComposerDraft({ followUpOptions, maxBytes: MAX_QUESTION_BYTES, maxHeight: MAX_INPUT_H })
  const {
    draft, setDraft,
    picked: pickedOptions, toggleOption, mergeIntoDraft, exceedsByteLimit,
  } = composer

  /** Wrapper around the native composer; the Select-to-Ask seed resolves the
   *  textarea through it (`textarea[data-composer-input]`) instead of a
   *  dedicated ref prop on ChatInput. */
  const composerWrapRef = useRef<HTMLDivElement | null>(null)

  // Drain any text a cancel released, from EITHER convergence path. Merged, not
  // replaced: the released text has no other home once the server let it go, and
  // an in-progress draft is typed work too — so neither may be discarded.
  // queue_id -> the RAW text this client submitted for it. Broadcast payloads
  // are redacted on the wire, so for a credential-bearing question the frames
  // are the wrong source to restore from; the submission itself is the only
  // place the raw text exists on the client. A ref, not state: it feeds an
  // event handler and must never trigger a render.
  const submittedRaw = useRef<Map<string, string>>(new Map())
  const releasedText = reduxSide?.releasedText
  // The release this component has already merged, so draining is idempotent.
  // Appending to the draft is not a safe thing to repeat, and the dispatch below
  // cannot be relied on to prevent a repeat: StrictMode runs an effect, its cleanup,
  // then the effect AGAIN against the same render's closure, so the store has not
  // changed in between and the question would land in the composer twice.
  //
  // Keyed by slot + text and cleared as soon as the store holds no release, so this
  // can only ever suppress a re-run for a release still in flight — never a genuine
  // second release of the same text. Skipping that would swap a visible duplicate for
  // a silent loss, and losing a submit is the one outcome this feature must not have.
  const mergedRelease = useRef<string | null>(null)
  useEffect(() => {
    if (!releasedText) {
      mergedRelease.current = null
      return
    }
    // Joined rather than a template literal: the i18n gate treats an interpolated
    // literal in a .tsx as user-visible copy, and this is an internal key.
    const key = [slot, releasedText].join('\u0000')
    if (mergedRelease.current === key) return
    mergedRelease.current = key
    mergeIntoDraft(releasedText)
    // Report WHAT was drained, so a cancel that appended after this render keeps
    // its text instead of being cleared along with it.
    dispatch(sideReleaseConsumed({ slot, consumed: releasedText }))
  }, [releasedText, slot, dispatch, mergeIntoDraft])

  // A requeued steer's card arrives from the socket with its content REDACTED, and the
  // reducer's cancel path releases the card's own content — it cannot reach the raw-text
  // map, which lives in a ref here. Rewrite the card as soon as both halves are known.
  // Fixing the CARD rather than each reader is what makes every path correct at once.
  const sideQueue = reduxSide?.queue
  useEffect(() => {
    if (!sideQueue?.length) return
    for (const entry of sideQueue) {
      if (!entry.steerId) continue
      const raw = submittedRaw.current.get(STEER_RAW_PREFIX + entry.steerId)
      if (raw && entry.content !== raw) {
        dispatch(sseSideQueue({ slot, action: 'edit', queue_id: entry.id, content: raw, raw: true }))
      }
    }
  }, [sideQueue, slot, dispatch])

  const sendMutation = useMutation({
    mutationFn: async ({ q, steer, slot: target }: SideSubmit) => {
      await api.sideOpen(target)
      return api.sideTurn(target, q, steer ? { steer: true } : undefined)
    },
    onMutate: ({ q, optimistic, slot: target, override }: SideSubmit) => {
      setLocalError(null)
      setLocalNotice(null)
      if (optimistic) {
        const message: SideMessage = { role: 'user', content: q, ts: new Date().toISOString() }
        dispatch(sideOptimisticAppend({ slot: target, message }))
      }
      // Only a composer submit owns the composer's text. An override send carries its own
      // text, so clearing here would throw away a draft the user has not sent yet.
      if (!override) setDraft('')
    },
    onSuccess: (res, vars) => {
      // Same two-path convergence as cancel/edit: a queued submit's card comes
      // from whichever of the HTTP response and the WS frame lands first, so a
      // dropped socket cannot leave the queue invisible. `front` is deliberately
      // NOT set — an ordinary submit goes to the tail, and only the backend's own
      // head-inserts (requeued steers, failed drains) carry it.
      // `still_queued` is the server's answer to "does this entry exist right
      // now": a turn that ended during the request may already have drained it,
      // and synthesising a card for a drained entry shows one that 404s on
      // cancel. An older backend omits the field, so treat absent as true and
      // keep the round-4 behaviour rather than silently dropping the card.
      const stillQueued = res.still_queued ?? true
      if (res.queued && res.queue_id) {
        // Record the raw text even when the entry has already drained: a cancel
        // racing the response still needs somewhere honest to restore from.
        submittedRaw.current.set(res.queue_id, vars.q)
        // Bounded by SIZE, not by whether the entry is still queued. Pruning on
        // departure would race the very thing this map exists to survive: the WS
        // frame removes the entry, the prune runs, and the HTTP handler then
        // finds nothing. Insertion order is preserved, so the oldest goes first.
        while (submittedRaw.current.size > MAX_SUBMITTED_RAW) {
          const oldest = submittedRaw.current.keys().next().value
          if (oldest === undefined) break
          submittedRaw.current.delete(oldest)
        }
      }
      // A steer whose consumption is unproven may still become a queue card, at the
      // turn's end or on an auth hold. That card gets a brand-new id and REDACTED
      // content, so record the raw text under the steer's own ledger id now — it is
      // the only handle that survives into the card.
      // Keyed on HAVING a handle, not on the outcome still being open. A steer whose
      // card the turn already made comes back `demoted`, not `pending`, and that reply
      // is the only place its ledger id is ever offered.
      if (res.steer_id) {
        submittedRaw.current.set(STEER_RAW_PREFIX + res.steer_id, vars.q)
        const correlated = res.steer_id
        setCorrelatedSteerIds(prev => {
          const next = new Set(prev)
          next.add(correlated)
          // Bounded like `submittedRaw`, oldest first — insertion order is Set order.
          while (next.size > MAX_SUBMITTED_RAW) {
            const oldest = next.values().next().value
            if (oldest === undefined) break
            next.delete(oldest)
          }
          return next
        })
        while (submittedRaw.current.size > MAX_SUBMITTED_RAW) {
          const oldest = submittedRaw.current.keys().next().value
          if (oldest === undefined) break
          submittedRaw.current.delete(oldest)
        }
      }
      // If the requeued card already arrived on the socket it holds the REDACTED
      // copy, and the reducer's own cancel path reads the card, not this map. Repair
      // the card now so every consumer sees raw text.
      if (res.steer_id) {
        const existing = reduxSide?.queue?.find(e => e.steerId === res.steer_id)
        if (existing && existing.content !== vars.q) {
          dispatch(sseSideQueue({ slot: vars.slot, action: 'edit', queue_id: existing.id, content: vars.q, raw: true }))
          setCorrelatedQueueIds(prev => new Set(prev).add(existing.id))
        }
      }
      // The client guessed idle (`optimistic: !isBusy`) but the SERVER is
      // authoritative and says it queued this. They disagree after a reload, before
      // the first frame restores `streaming`. Retract the bubble: while queued, the
      // question belongs to its queue card, and claiming a transcript row would say
      // it had already been asked.
      if (res.queued && vars.optimistic) dispatch(sideOptimisticRollback(vars.slot))
      if (res.queued && res.queue_id && stillQueued) {
        dispatch(sseSideQueue({ slot: vars.slot, action: 'push', queue_id: res.queue_id, content: vars.q, raw: true }))
        // Then repair the content to the RAW text. If the redacted WS frame created
        // this card first, the push above was ignored as a duplicate (it must be, or
        // a late redacted push would clobber raw text) and the card would still hold
        // the scrubbed rendering — which a cancel would hand back to the composer.
        // `edit` is the one action allowed to change content, so this lands the raw
        // copy whichever channel won the race.
        dispatch(sseSideQueue({ slot: vars.slot, action: 'edit', queue_id: res.queue_id, content: vars.q, raw: true }))
        if (res.queue_id) setCorrelatedQueueIds(prev => new Set(prev).add(res.queue_id as string))
      }
      // A steer the server could not deliver becomes a queue entry. Say so:
      // otherwise the only signal that Steer turned into Queue is a card the user
      // has to notice on their own.
      if (res.demoted) setLocalNotice(i18nT('pages.chat.sideChat.steer_demoted_to_queue'))
    },
    onError: (_err, vars) => {
      // `optimistic` rides along in the vars rather than being recomputed here:
      // dispatching the bubble flips the side to busy, so re-deriving it in this
      // callback would read the post-submit state and skip the rollback.
      if (vars.optimistic) dispatch(sideOptimisticRollback(vars.slot))
      // Nothing was accepted, so hand the text back — merged, not chosen: the
      // user may have started a new draft while the request was in flight.
      mergeIntoDraft(vars.q)
    },
  })

  // Queue ids whose cancel/edit is in flight. The card is only retired when the
  // server's frame lands, so without this a second click fires a duplicate that
  // races the first and returns 404 — reporting a failure for an action that
  // worked. Tracked per id rather than as one flag so two cards stay independent.
  const [pendingQueueIds, setPendingQueueIds] = useState<ReadonlySet<string>>(() => new Set())
  // Steer ids whose raw text this client has cached. Mirrors the `steer:` keys in
  // `submittedRaw`, which is a ref and so cannot re-render the card when it fills.
  const [correlatedSteerIds, setCorrelatedSteerIds] = useState<ReadonlySet<string>>(() => new Set())
  // Queue ids whose raw text this client has cached, for the same reason `correlatedSteerIds`
  // exists: `submittedRaw` is a ref and cannot re-render a card when it fills.
  const [correlatedQueueIds, setCorrelatedQueueIds] = useState<ReadonlySet<string>>(() => new Set())
  const markQueuePending = useCallback((queueId: string, pending: boolean) => {
    setPendingQueueIds(prev => {
      if (pending === prev.has(queueId)) return prev
      const next = new Set(prev)
      if (pending) next.add(queueId)
      else next.delete(queueId)
      return next
    })
  }, [])

  // Cancel and edit are SERVER-AUTHORITATIVE: the card changes only once the
  // server has confirmed. A drain can dequeue the entry between render and click,
  // so an optimistic update would claim the text was cancelled while the turn it
  // started is already running — the one divergence a queue card must never show.
  //
  // Confirmation arrives by TWO independent paths: the HTTP response here and the
  // `chat.side_queue` frame. Both dispatch the same replay-safe reducer action, so
  // whichever lands first wins and the other is a no-op — a dropped WebSocket can
  // no longer leave a card stale forever.
  const cancelQueued = useMutation({
    mutationFn: ({ queueId, slot: target }: { queueId: string; slot: string }) =>
      api.sideQueueCancel(target, queueId),
    onMutate: ({ queueId }: { queueId: string; slot: string }) => { markQueuePending(queueId, true) },
    onSuccess: (res, { queueId, slot: target }) => {
      // The reducer stashes the released text and the effect above drains it, so
      // this path and the WS frame share ONE release — restoring the draft here
      // as well would double-append it.
      // Prefer the raw text THIS client submitted. Both the card and the frame can
      // hold a redacted rendering (broadcasts are scrubbed on the wire), and for a
      // credential-bearing question that is a permanently corrupted restore. The
      // submission is the only place the raw text still exists here.
      // A requeued steer's card was never submitted AS a queue entry, so nothing is
      // stored under its id — fall back to the steer it came from, which the card
      // carries precisely so this lookup can succeed.
      const entry = reduxSide?.queue?.find(e => e.id === queueId)
      const raw = submittedRaw.current.get(queueId)
        ?? (entry?.steerId ? submittedRaw.current.get(STEER_RAW_PREFIX + entry.steerId) : undefined)
      dispatch(sseSideQueue({
        slot: target,
        action: 'cancel',
        queue_id: queueId,
        content: raw ?? res.content,
        // Vouch ONLY for a copy this client actually holds. `res.content` comes from the
        // server and is scrubbed, so marking that raw would defeat the preference.
        ...(raw !== undefined ? { raw: true } : {}),
      }))
      submittedRaw.current.delete(queueId)
      if (entry?.steerId) submittedRaw.current.delete(STEER_RAW_PREFIX + entry.steerId)
    },
    onError: () => {
      setLocalError(i18nT('pages.chat.sideChat.queue_cancel_failed'))
    },
    onSettled: (_d, _e, { queueId }) => { markQueuePending(queueId, false) },
  })

  const editQueued = useMutation({
    mutationFn: ({ queueId, content, slot: target }: { queueId: string; content: string; slot: string }) =>
      api.sideQueueEdit(target, queueId, content),
    onMutate: ({ queueId, slot: target }: { queueId: string; content: string; slot: string }) => {
      markQueuePending(queueId, true)
      return { broadcastAt: queueEditBroadcastAt(target, queueId) }
    },
    onSuccess: (_res, vars) => {
      // The edit supersedes what this client had cached for the card. A cancel PREFERS
      // the cached copy over the card's content, so leaving it stale would restore the
      // pre-edit text and throw the user's newer wording away.
      //
      // Any `steer:<id>` fallback for the same card is deliberately left in place. It is
      // only consulted once this entry has been evicted, and at that point it holds the
      // last unredacted copy of the question — pre-edit raw text beats handing the
      // composer the card's scrubbed rendering.
      submittedRaw.current.set(vars.queueId, vars.content)
      dispatch(sseSideQueue({ slot: vars.slot, action: 'edit', queue_id: vars.queueId, content: vars.content, raw: true }))
    },
    onError: (_err, vars, ctx) => {
      // The server broadcast an edit for this card after the request went out, so the edit DID
      // land and only its response was lost. Restoring here would leave the question both
      // queued and in the composer, and it would be asked twice. The cache is refreshed
      // instead, because a later cancel prefers it and the server now holds this wording.
      if (queueEditBroadcastAt(vars.slot, vars.queueId) > (ctx?.broadcastAt ?? 0)) {
        submittedRaw.current.set(vars.queueId, vars.content)
        return
      }
      setLocalError(i18nT('pages.chat.sideChat.queue_edit_failed'))
      // The editor is already closed (it closes on save, before the request resolves), so
      // this text has nowhere else to live: a 404 means the entry drained and its card is
      // gone, and a surviving card still shows the pre-edit content. Merge, never assign —
      // the composer may hold a question the user has since started typing.
      mergeIntoDraft(vars.content)
    },
    onSettled: (_d, _e, vars) => { markQueuePending(vars.queueId, false) },
  })

  /** Queue entries in the shape QueueStack renders, so the side panel and the
   *  main composer show one card design rather than two. */
  /** Cards whose actions must not fire: a request is in flight, or the card came from a
   *  steer whose raw text this client cannot name yet, so cancelling would release the
   *  scrubbed broadcast copy instead of the question. */
  const blockedQueueIds = useMemo<ReadonlySet<string>>(() => {
    const blocked = new Set(pendingQueueIds)
    for (const entry of queue) {
      if (entry.steerId && !correlatedSteerIds.has(entry.steerId)) blocked.add(entry.id)
      // A submit in flight may already have produced this card via the scrubbed push, with its
      // raw text still travelling in the response. Bounded by the request rather than by
      // "uncorrelated", which would permanently freeze another tab's cards and anything
      // predating a refresh.
      if (sendMutation.isPending && !correlatedQueueIds.has(entry.id)) blocked.add(entry.id)
      // Text this client never held. Editing it would save the scrubbed rendering over the
      // real question, and CANCELLING it deletes the raw entry server-side while the response
      // hands back only `redact(content)` — so once the tab that typed it has closed, the
      // question would survive nowhere. The entry still drains on the next turn.
      if (entry.raw !== true) blocked.add(entry.id)
    }
    return blocked
  }, [pendingQueueIds, queue, correlatedSteerIds, correlatedQueueIds, sendMutation.isPending])

  const queueCards = useMemo<ChatMessage[]>(
    () => queue.map(e => ({ role: 'queued', content: e.content, cls: 'msg msg-q', ts: e.ts, meta: { queueId: e.id } })),
    [queue]
  )

  const refreshMutation = useMutation({
    // local close is the source of truth — backend close errors are
    // intentionally not surfaced (the side state is gone locally either way).
    mutationFn: ({ slot: target }: { slot: string }) => api.sideClose(target),
    onMutate: ({ slot: target }: { slot: string }) => {
      dispatch(sideClose(target))
    },
  })

  // Scroll follow lives in useChatScrollFollow (wired on the scroller below);
  // no tail-keyed effect — the hook's ResizeObserver sees every height change.

  // Select-to-Ask seed: when the user clicks "Ask" in the selection toolbar,
  // ChatPage opens this panel and fires a `side-seed` CustomEvent carrying the
  // selected text. Prefill the draft with the selection as a grounding
  // blockquote and focus the input so the user types their actual question
  // (which then fires sideOpen → sideTurn as usual). Isolated from main context.
  useEffect(() => {
    const onSeed = (e: Event) => {
      const detail = (e as CustomEvent<{ text?: string }>).detail
      const sel = detail?.text?.trim()
      if (!sel) return
      const quoted = sel.split('\n').map(line => `> ${line}`).join('\n')
      setDraft(prev => (prev.trim() ? `${prev.trimEnd()}\n\n${quoted}\n\n` : `${quoted}\n\n`))
      // Focus + place caret at the end so the user immediately types the question.
      requestAnimationFrame(() => {
        const el = composerWrapRef.current?.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
        if (el) {
          el.focus()
          const len = el.value.length
          el.setSelectionRange(len, len)
          // Scroll to the top so the START of a long quote is visible (focusing
          // + caret-at-end scrolls to the bottom otherwise, hiding the quote).
          el.scrollTop = 0
        }
      })
    }
    window.addEventListener('side-seed', onSeed)
    return () => window.removeEventListener('side-seed', onSeed)
    // `setDraft` comes from the SDK hook, so its stability is not something the
    // linter can see for itself — declared rather than assumed.
  }, [setDraft])

  // Auto-grow of the input is the SDK hook's job — the `min-h-[52px]` class below
  // still floors an empty box at ~2 rows, so this surface keeps its own resting size.

  /** `override` carries the text a follow-up chip's send arrow supplies; without it the draft
   *  is the source of truth. Every call site wraps this in an arrow, so a click event can never
   *  arrive here as the override. */
  const send = useCallback((override?: string, steerRequested = false) => {
    const q = (override ?? draft).trim()
    if (!q || sendMutation.isPending || !slot) return
    if (exceedsByteLimit(q)) {
      // The limit is enforced in UTF-8 bytes (server contract), but a byte count is
      // not actionable to the user — report a character target instead, derived
      // from THIS text's own byte density rather than a fixed worst-case (4
      // bytes/char) floor. The fixed floor over-instructed deletion for every
      // script but all-emoji: an ASCII user was told to cut to ~8,192 chars when
      // trimming one character would do, and zh-CN (3 bytes/char) was told 8,192
      // when ~10,922 chars actually fit. Same accuracy the all-emoji case already
      // had (still 8,192 there, since emoji sit at the 4-byte floor) — just no
      // longer wrong for everything else.
      const chars = [...q].length
      const bytes = draftByteSize(q)
      const max = Math.floor((chars * MAX_QUESTION_BYTES) / bytes)
      setLocalError(i18nT('pages.chat.sideChat.question_too_long', {
        max: fmtNumber(max),
        current: fmtNumber(chars),
      }))
      return
    }
    // While a turn runs, ChatInput's split button decides: steer injects into
    // it, queue defers. From idle both collapse to "start a turn", so the flag
    // is dropped. An optimistic bubble belongs only to a turn this submit
    // STARTS — a steer's bubble has to land above the streaming answer and a
    // queued one is a card, so the server frame places both.
    const steer = isBusy && steerRequested
    sendMutation.mutate({ q, steer, optimistic: !isBusy, slot, override: override != null })
  }, [draft, slot, sendMutation, isBusy, exceedsByteLimit])

  const sendErr = sendMutation.error
  const displayError = sendErr
    ? (sendErr instanceof Error ? sendErr.message : String(sendErr))
    : localError

  const turnsBehind = reduxSide ? parentTurnCount - reduxSide.openedAtTurnCount : 0
  const age = reduxSide?.createdAt ? relativeTime(reduxSide.createdAt) : null
  const showBanner = !!reduxSide && messages.length > 0
  const isStale = turnsBehind >= 10 || (reduxSide?.createdAt && Date.now() - new Date(reduxSide.createdAt).getTime() >= 4 * 3600_000)

  // `slot` is read in the body, so it is declared. No stale capture is being fixed
  // here: `refreshMutation` is already a dep and `useMutation` hands back a fresh
  // object every render, so this callback is rebuilt every render either way and has
  // no stability worth protecting. `send` above declares it for the same reason.
  const handleRefresh = useCallback(() => {
    refreshMutation.mutate({ slot })
  }, [refreshMutation, slot])

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {showBanner && (
        <div className={`flex items-center justify-between px-3 py-1.5 text-[12px] border-b border-border shrink-0 ${isStale ? 'bg-warn/10 text-warn' : 'bg-bg-hover/50 text-muted'}`}>
          <span className="italic">
            {i18nT('pages.chat.sideChat.context_from')} {i18nT('pages.chat.sideChat.turn', { count: turnsBehind })} {i18nT('pages.chat.sideChat.ago')}{age ? ` · ${age}` : ''}
          </span>
          <button
            onClick={() => void handleRefresh()}
            title={
              queue.length > 0
                ? i18nT('pages.chat.sideChat.refresh_blocked_queued')
                : isBusy
                  ? i18nT('pages.chat.sideChat.refresh_blocked_busy')
                  : undefined
            }
            // Closing the sidecar clears the queue AND the steer ledger. A queued question,
            // an undeliverable steer parked in that list, and an accepted steer still waiting
            // to be consumed are all discarded — the last one is invisible here, which is why
            // a running turn blocks too: its unconsumed steer only survives via the requeue
            // that closing skips.
            disabled={refreshMutation.isPending || queue.length > 0 || isBusy}
            className="flex items-center gap-1 text-[11px] font-medium text-accent hover:text-accent-hover disabled:opacity-50 bg-transparent border-none cursor-pointer disabled:cursor-not-allowed"
          >
            <RotateCcw size={11} className={refreshMutation.isPending ? 'animate-spin' : ''} />
            {i18nT('pages.chat.sideChat.refresh_context')}
          </button>
        </div>
      )}
      <div ref={scrollRef} onScroll={follow.onScroll} className="flex-1 overflow-y-auto px-3 py-2">
        <div ref={follow.contentRef} className="space-y-2">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted gap-2 py-8">
            {/* The icon is decoration and stays faint; the sentence is the only
                place the UI states that this transcript is discarded, so it reads
                at full muted contrast rather than inheriting the icon's /30. */}
            <span className="text-[24px] text-muted/30"><MessageCircleQuestionMark className="lucide-inline" /></span>
            <span className="text-[13px]">{i18nT('pages.chat.sideChat.ask_a_side_question_main_agent_keeps_working')}</span>
          </div>
        ) : (
          <ChatMessageList messages={transcript} running={isBusy} />
        )}
        {isPending && lastMsg?.role === 'user' && (
          <div className="flex items-center gap-1.5 px-2.5 py-2 text-muted">
            <span className="flex gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '0ms' }} />
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '150ms' }} />
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '300ms' }} />
            </span>
            <span className="text-[12px] streaming-indicator">{i18nT('pages.chat.sideChat.thinking')}</span>
          </div>
        )}
        </div>
      </div>
      {displayError && (
        <div className="px-3 py-1 text-[12px] text-danger border-t border-border">{displayError}</div>
      )}
      {!displayError && localNotice && (
        <div className="px-3 py-1 text-[12px] text-muted border-t border-border" role="status">{localNotice}</div>
      )}
      {queueCards.length > 0 && (
        <div className="shrink-0 pt-1">
          <QueueStack
            messages={queueCards}
            fuseBelow={false}
            pendingIds={blockedQueueIds}
            onCancel={qid => { if (!blockedQueueIds.has(qid)) cancelQueued.mutate({ queueId: qid, slot }) }}
            onEdit={(qid, content) => { if (!blockedQueueIds.has(qid)) editQueued.mutate({ queueId: qid, content, slot }) }}
          />
        </div>
      )}
      {followUpOptions.length > 0 && (
        <div className="shrink-0 px-2 pb-1">
          <FollowUpBar
            options={followUpOptions}
            recommended={followUpRecommended}
            picked={pickedOptions}
            onSelect={toggleOption}
            onSend={text => { void send(text) }}
          />
        </div>
      )}
      {/* The REAL native composer, scoped to this session's slot. The wrapper
          carries the Select-to-Ask mount signal (an attribute, not the
          aria-label: the label is translated, so a selector built from its
          English text matches in one language out of twelve); the seed handler
          resolves the textarea through a wrapper query. Capability shaping is by
          omission: no upload/voice/agent/model props, so the slim surface
          renders none of that chrome — same component, fewer capabilities. */}
      <div ref={composerWrapRef} data-side-chat-input="" className="border-t border-border p-2 shrink-0">
        <SlotProvider slotId={slot}>
          <ChatInput
            value={draft}
            onChange={setDraft}
            onSend={() => { void send() }}
            canSteer
            onSteer={() => { void send(undefined, true) }}
            isRunning={isBusy}
            placeholder={i18nT('pages.chat.sideChat.ask_a_side_question_2')}
            inputAriaLabel={i18nT('pages.chat.sideChat.ask_a_side_question')}
            typedCommandMenus={false}
            slotApprovalChrome={false}
            promptOptimizer={false}
            connected={connected}
          />
          <div role="note" className="px-1 pt-1.5 text-[11px] leading-4 text-muted">
            {i18nT('pages.chat.sideChat.context_only_tools_unavailable')}
          </div>
        </SlotProvider>
      </div>
    </div>
  )
}
