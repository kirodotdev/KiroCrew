/**
 * The pending `ask_question` card of one slot, for `ChatEmbed`.
 *
 * The dashboard's `PendingQuestionCard` reads the card from the Redux store the
 * owner's websocket fills, and answers through `/api/ask-question/*`. Neither is
 * there for an embed: it owns no store, `question_card` frames go to the
 * dashboard owner's sockets only, and the `/api/ask-question/*` routes refuse
 * every app token. So this card reads and answers through the slot's own routes,
 * which admit the slot's owner app (with `permissions.sessionApproval`, the grant
 * approvals need) and nobody else's:
 *
 * - `GET  /api/chat/slots/{slot}/question`           the slot's pending cards
 * - `POST /api/chat/slots/{slot}/question/answer`    answer or decline a BLOCKING card
 * - `POST /api/chat/slots/{slot}/question/steer`     steer a NATIVE card's answer
 * - `POST /api/chat/slots/{slot}/question/dismiss`   retire a NATIVE card's status
 *
 * Two kinds of card are shown. A BLOCKING card (an `ask_id`, the MCP
 * `ask_question` tool) parks a wait; its answer goes to `/answer`. A NATIVE card
 * (kiro-cli's mid-turn `AskUserQuestion`: `native: true`, a `card_id`, no
 * `ask_id`) is raised while its own turn is still running and waiting on the
 * answer, so a plain send would queue behind that turn and the question would
 * never be consumed. Its answer STEERS into the running turn (`/steer`, with the
 * app's own provenance server-side), exactly as the dashboard's
 * `onDirectQuestionSend` steers it. When the turn has already ended the steer is
 * `unavailable`, and the answer goes back to the composer so it is never lost.
 *
 * The rendering is the shared `QuestionCard`. A 404 on an answer means the wait
 * is gone, so the typed answer goes back to the composer, unsent. So does a
 * draft whose card retires (expired, withdrawn) before it was submitted.
 */
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import QuestionCard from '../components/QuestionCard'
import ErrorNotice from '../components/ErrorNotice'
import StatusNotice from '../components/StatusNotice'
import { i18nT } from '../i18n/t'
import { AcceptedBodyUnreadable, ApiError } from '../api/apiError'
import { answerFailureKey, answerRestoredNotice, answersAsText } from '../utils/questionAnswers'
import { answeredByIntermediary } from '../utils/sendDelivery'
import type { AppApi } from './scopedApi'

interface EmbedQuestion {
  question: string
  header?: string
  options: { label: string; description?: string }[]
  multiSelect?: boolean
}

/** One entry of `GET /api/chat/slots/{slot}/question`.
 *  A blocking card carries an `ask_id`; a native mid-turn card carries a
 *  `card_id` and `native: true` and no `ask_id`. */
export interface EmbedPendingQuestion {
  /** Set only on a blocking card (the MCP `ask_question` tool's wait). */
  ask_id?: string
  /** Set only on a native mid-turn card (`AskUserQuestion`). */
  card_id?: string
  /** True on a native mid-turn card; its answer steers into the running turn. */
  native?: boolean
  slot: string
  questions: EmbedQuestion[]
  ts?: number
}

/** The stable identity a card is keyed by: its `ask_id`, or a native card's
 *  `card_id`. Both routes key drafts, in-flight sends and settled sets by it. */
const cardIdentity = (c: { ask_id?: string; card_id?: string } | null | undefined): string =>
  (c?.ask_id || c?.card_id || '')

/** A native card has no `ask_id`: its answer steers rather than resolving a wait. */
const isNative = (c: { ask_id?: string; native?: boolean } | null | undefined): boolean =>
  !!c && !c.ask_id && c.native === true

/** Poll cadence while a turn runs (a blocking ask parks inside one) and while idle. */
export const EMBED_QUESTION_POLL_RUNNING_MS = 1500
export const EMBED_QUESTION_POLL_IDLE_MS = 10_000

export const embedQuestionPath = (slot: string) => `/api/chat/slots/${encodeURIComponent(slot)}/question`
const queryKey = (slot: string) => ['app-sdk-embed-question', slot]

/** A refusal that will not change by asking again: no grant, no API permission.
 *  Not a 404: a slot the embed mounts before its first send does not exist yet,
 *  and the send that creates it must not leave the card unpolled. */
function isStandingRefusal(err: unknown): boolean {
  // A 403 that asks for sign-in is a lapsed session, not a refusal: the poll
  // keeps running so it recovers once the session is refreshed.
  if (err instanceof ApiError) return err.status === 403 && !err.authRequired
  return err instanceof Error && err.name === 'AppApiPermissionError'
}
/** No slot by that name (yet, or for this app): the same as no card. */
const isNoSlot = (err: unknown) => err instanceof ApiError && err.status === 404

export interface EmbedQuestionState {
  /** The card an agent is waiting on, or null. */
  pending: EmbedPendingQuestion | null
  /** The question read failed for a reason that may pass (offline, 5xx). */
  loadFailed: boolean
}

/** The slot's pending blocking card, polled. Also drives the embed's follow-up chips. */
export function useEmbedPendingQuestion(api: AppApi, slot: string, running: boolean): EmbedQuestionState {
  const { data, status, error } = useQuery({
    queryKey: queryKey(slot),
    queryFn: async () => {
      const res = await api.get<{ pending?: Partial<EmbedPendingQuestion>[] }>(embedQuestionPath(slot))
      const rows = Array.isArray(res?.pending) ? res.pending : []
      // Keep a blocking card (a non-empty `ask_id`) and a native mid-turn card
      // (a non-empty `card_id` + `native: true`, no `ask_id`); both must carry
      // questions to render. A status-only record (no questions) is skipped.
      return rows.filter((c): c is EmbedPendingQuestion => {
        if (!Array.isArray(c.questions) || c.questions.length === 0) return false
        if (typeof c.ask_id === 'string' && c.ask_id) return true
        return typeof c.card_id === 'string' && !!c.card_id && c.native === true
      })
    },
    // A refusal is the answer for this app and slot, so it is not retried or
    // polled again: an app without the grant simply shows no card.
    retry: (count, err) => !isStandingRefusal(err) && !isNoSlot(err) && count < 2,
    refetchInterval: (query) =>
      query.state.status === 'error' && isStandingRefusal(query.state.error)
        ? false
        : running ? EMBED_QUESTION_POLL_RUNNING_MS : EMBED_QUESTION_POLL_IDLE_MS,
  })
  // A refusal or a missing slot is the current answer, whatever an earlier read
  // held: a card the app may no longer answer must not stay on screen.
  const refused = status === 'error' && (isStandingRefusal(error) || isNoSlot(error))
  const cards = refused ? [] : data ?? []
  // The newest card is the one an agent is waiting on.
  const pending = cards.length ? cards.reduce((a, b) => ((b.ts ?? 0) >= (a.ts ?? 0) ? b : a)) : null
  return { pending, loadFailed: status === 'error' && !isStandingRefusal(error) && !isNoSlot(error) }
}

interface EmbedQuestionCardProps {
  api: AppApi
  slot: string
  /** The card to show, or null. Mounted either way, so a notice about where an
   *  answer went outlives the card it was for. */
  pending: EmbedPendingQuestion | null
  loadFailed: boolean
  /** Hand an undeliverable answer for `slot` back to the composer, merged with
   *  its draft. Returns false when no embed for that slot is mounted any more and
   *  the caller parked it instead (`notice` then travels with it). */
  onRestore: (slot: string, text: string, notice: string) => boolean
}

export default function EmbedQuestionCard({ api, slot, pending, loadFailed, onRestore }: EmbedQuestionCardProps) {
  const queryClient = useQueryClient()
  const identity = cardIdentity(pending)
  const [busyFor, setBusyFor] = useState<string | null>(null)
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null)
  // Cards this component already settled, so the next poll cannot repaint one.
  const [settled, setSettled] = useState<ReadonlySet<string>>(() => new Set())
  // Where an undeliverable answer went. Not tied to a card: that card is gone.
  // `failed`: a submit was refused or did not go out (an error). Otherwise the
  // card retired by itself and its draft was kept (a status, nothing failed).
  const [restored, setRestored] = useState<{ message: string; failed: boolean } | null>(null)
  // The answers typed into each card, kept outside QuestionCard so they survive
  // a remount and can be handed back when the card retires unsubmitted.
  const drafts = useRef(new Map<string, Record<string, string>>())
  // Asks whose answer POST is out. Their outcome, not a poll, decides what
  // happens to the typed answer: a poll can retire the card first (the server
  // resolves the ask before it replies), and every poll hands QuestionCard a
  // fresh question array, which re-fires its draft callback.
  const inFlight = useRef(new Set<string>())
  const shown = useRef<EmbedPendingQuestion | null>(null)
  const restoreRef = useRef(onRestore)
  restoreRef.current = onRestore
  // An unmounted card shows nothing: a POST that fails after teardown must take
  // the hand-back path (which parks the answer for its slot), not keep it as a
  // draft in a map nobody reads again.
  useEffect(() => () => { shown.current = null }, [])

  // A card that left the poll without being submitted here (expired, withdrawn,
  // answered elsewhere) takes its draft with it unless the draft is handed back.
  useEffect(() => {
    const previous = shown.current
    shown.current = pending
    const prevId = cardIdentity(previous)
    if (!prevId || prevId === cardIdentity(pending)) return
    if (inFlight.current.has(prevId)) return
    const draft = drafts.current.get(prevId)
    drafts.current.delete(prevId)
    if (!draft || !previous) return
    const text = answersAsText(draft)
    if (!text.trim()) return
    const message = answerRestoredNotice(previous.questions)
    if (restoreRef.current(previous.slot, text, message)) setRestored({ message, failed: false })
  }, [pending])

  const busy = busyFor === identity
  const base = embedQuestionPath(slot)

  const settle = (id: string) => {
    drafts.current.delete(id)
    setSettled(prev => new Set(prev).add(id))
    queryClient.setQueryData<EmbedPendingQuestion[]>(queryKey(slot), prev =>
      (prev ?? []).filter(c => cardIdentity(c) !== id))
    void queryClient.invalidateQueries({ queryKey: queryKey(slot) })
  }

  const notice = failure && failure.id === identity ? failure.message : null
  const sideNotices = (
    <>
      {restored && (
        <div className="shrink-0 px-3 pt-2">
          {restored.failed ? (
            /* No hand-off: the answer is in the composer below, unsent; the
               hand-off's navigation would discard that draft. */
            <ErrorNotice message={restored.message} onDismiss={() => setRestored(null)} testId="chat-embed-question-restored" />
          ) : (
            <StatusNotice message={restored.message} onDismiss={() => setRestored(null)} testId="chat-embed-question-restored" />
          )}
        </div>
      )}
      {loadFailed && (
        <div className="shrink-0 px-3 pt-2">
          {/* No hand-off: the embed composer's unsent draft is local state the
              hand-off's navigation would discard; the poll retries on its own. */}
          <ErrorNotice message={i18nT('components.pendingQuestionCard.load_failed')} testId="chat-embed-question-load-failed" />
        </div>
      )}
    </>
  )
  if (!pending || settled.has(identity)) return sideNotices

  const answer = (answers: Record<string, string> | undefined) => {
    // A native mid-turn card steers its answer into the running turn; a blocking
    // ask resolves its wait through /answer. The branch is the only difference —
    // drafts, in-flight tracking and settling are keyed by the same identity.
    if (isNative(pending)) { steerNative(answers); return }
    const askId = cardIdentity(pending)
    if (busy) return
    inFlight.current.add(askId)
    drafts.current.delete(askId)
    setRestored(null)
    setBusyFor(askId)
    setFailure(null)
    api.post(`${base}/answer`, answers ? { ask_id: askId, answers } : { ask_id: askId, dismissed: true })
      .then(() => { inFlight.current.delete(askId); settle(askId) })
      .catch((err) => {
        inFlight.current.delete(askId)
        // A 2xx whose body could not be read: the server took the answer, so it
        // is settled like a success and never handed back as unsent.
        // Unless the 2xx was written by something in front of the gateway (an
        // auth proxy's login page): then nothing was delivered, and the answer
        // takes the failure path below.
        // (The same provenance rule the chat send path applies, utils/sendDelivery.)
        if (err instanceof AcceptedBodyUnreadable && !(err.response && answeredByIntermediary(err.response, `${base}/answer`))) {
          settle(askId)
          return
        }
        // The card stays editable while the POST is out, so the latest edit (kept
        // in `drafts` meanwhile) wins over the submitted snapshot: an edit typed
        // after Submit is never lost to a failed send.
        const edited = drafts.current.get(askId)
        if (edited && answersAsText(edited).trim()) answers = edited
        if (err instanceof ApiError && err.status === 404) {
          // The wait is gone (expired, withdrawn or answered elsewhere). The
          // typed text goes back to the composer; only the user sends it.
          settle(askId)
          const text = answers ? answersAsText(answers) : ''
          if (!text.trim()) return
          const message = answerRestoredNotice(pending.questions)
          if (restoreRef.current(slot, text, message)) setRestored({ message, failed: true })
          return
        }
        // Not delivered, and the agent may still be waiting: keep the answer as
        // this card's draft again, so a retry or a later retirement still has it.
        // If the card already left the poll while the POST was out, hand it back now.
        if (answers && answersAsText(answers).trim()) {
          if (cardIdentity(shown.current) === askId) drafts.current.set(askId, answers)
          else {
            const message = answerRestoredNotice(pending.questions)
            if (restoreRef.current(slot, answersAsText(answers), message)) setRestored({ message, failed: true })
            return
          }
        }
        setFailure({ id: askId, message: i18nT(answerFailureKey(err)) })
      })
      .finally(() => setBusyFor(current => (current === askId ? null : current)))
  }

  // A native mid-turn card: its answer must reach the RUNNING turn waiting on it.
  // `answers` undefined is a Dismiss, which retires the card's status
  // (`/dismiss`); otherwise the answer text steers into the turn (`/steer`).
  const steerNative = (answers: Record<string, string> | undefined) => {
    const cardId = cardIdentity(pending)
    if (busy) return
    const text = answers ? answersAsText(answers) : ''
    // A submit with no selected answer is a no-op, not a dismiss: the card stays.
    if (answers && !text.trim()) return
    inFlight.current.add(cardId)
    drafts.current.delete(cardId)
    setRestored(null)
    setBusyFor(cardId)
    setFailure(null)
    // Dismiss: retire the card's needs_input status; the agent reads nothing.
    if (!answers) {
      api.post(`${base}/dismiss`, { card_id: cardId })
        .then(() => { inFlight.current.delete(cardId); settle(cardId) })
        .catch((err) => {
          inFlight.current.delete(cardId)
          // A 2xx whose body could not be read: the gateway took the dismiss, so
          // it is settled — UNLESS the 2xx came from something in front of the
          // gateway (an auth proxy's login page), where nothing was retired and
          // settling would hide a card the next poll still returns. Same
          // provenance rule the answer/steer paths apply (utils/sendDelivery).
          if (err instanceof AcceptedBodyUnreadable) {
            if (err.response && answeredByIntermediary(err.response, `${base}/dismiss`)) {
              setFailure({ id: cardId, message: i18nT(answerFailureKey(err)) })
            } else {
              settle(cardId)
            }
            return
          }
          // A 404 means the card already retired (answered/replaced): same as done.
          if (err instanceof ApiError && err.status === 404) { settle(cardId); return }
          setFailure({ id: cardId, message: i18nT(answerFailureKey(err)) })
        })
        .finally(() => setBusyFor(current => (current === cardId ? null : current)))
      return
    }
    // Steer the answer into the running turn. The server keeps app provenance and
    // checks slot ownership; it replies `steered` when it landed, or
    // `unavailable` when no turn is running (the turn ended before Submit) so the
    // answer is handed back to the composer rather than dropped.
    api.post<{ ok?: boolean; steered?: boolean; unavailable?: boolean }>(`${base}/steer`, { card_id: cardId, message: text })
      .then((res) => {
        inFlight.current.delete(cardId)
        if (res && res.steered) { settle(cardId); return }
        // Not steered (no live turn): hand the answer to the composer, not lost.
        // The card stayed editable while the POST was out, so a later edit (kept
        // in `drafts`) wins over the submitted snapshot — captured BEFORE settle,
        // which deletes the draft. The card is cleared, so a later user send is
        // the only route left for it.
        const edited = drafts.current.get(cardId)
        const handBack = edited && answersAsText(edited).trim() ? answersAsText(edited) : text
        settle(cardId)
        const message = answerRestoredNotice(pending.questions)
        if (restoreRef.current(slot, handBack, message)) setRestored({ message, failed: false })
      })
      .catch((err) => {
        inFlight.current.delete(cardId)
        // The card stays editable while the POST is out, so the latest edit (kept
        // in `drafts` meanwhile) wins over the submitted snapshot.
        const edited = drafts.current.get(cardId)
        if (edited && answersAsText(edited).trim()) answers = edited
        // A 2xx whose body could not be read: for a STEER the body is the only
        // thing that says `steered` vs `unavailable`, so an unreadable one is
        // indeterminate — never settle-and-drop it. The agent may still be
        // waiting, so the answer is handed back to the composer (or kept as this
        // card's draft while the card is still shown), exactly like a
        // not-delivered outcome below. An intermediary 200 (auth proxy) is the
        // same: nothing reached the gateway.
        if (err instanceof AcceptedBodyUnreadable) {
          if (answers && answersAsText(answers).trim()) {
            if (cardIdentity(shown.current) === cardId) {
              // Card still shown: keep the latest answer as its draft AND
              // surface the failure, exactly as the /answer path does — an
              // unreadable 2xx is indeterminate, so the user must see it was not
              // confirmed rather than have the card silently sit as if sent.
              drafts.current.set(cardId, answers)
              setFailure({ id: cardId, message: i18nT(answerFailureKey(err)) })
              return
            }
            settle(cardId)
            const message = answerRestoredNotice(pending.questions)
            if (restoreRef.current(slot, answersAsText(answers), message)) setRestored({ message, failed: true })
          } else {
            settle(cardId)
          }
          return
        }
        if (err instanceof ApiError && err.status === 404) {
          // The card retired (the turn consumed or replaced it). Hand the typed
          // answer back to the composer so it is not lost.
          settle(cardId)
          const t = answers ? answersAsText(answers) : ''
          if (!t.trim()) return
          const message = answerRestoredNotice(pending.questions)
          if (restoreRef.current(slot, t, message)) setRestored({ message, failed: true })
          return
        }
        // Not delivered, agent may still wait: keep the draft (or hand back if the
        // card already left the poll), as the blocking path does.
        if (answers && answersAsText(answers).trim()) {
          if (cardIdentity(shown.current) === cardId) drafts.current.set(cardId, answers)
          else {
            const message = answerRestoredNotice(pending.questions)
            if (restoreRef.current(slot, answersAsText(answers), message)) setRestored({ message, failed: true })
            return
          }
        }
        setFailure({ id: cardId, message: i18nT(answerFailureKey(err)) })
      })
      .finally(() => setBusyFor(current => (current === cardId ? null : current)))
  }

  return (
    <>
    {sideNotices}
    {/* Shrinkable, unlike the strips around it: in a short embed the card gives
        up height before the composer does, and QuestionCard's own scroller (its
        body, never its footer) takes the slack, so Submit and Dismiss stay on
        screen. The first child is the card, allowed below its content height. */}
    <div className="flex min-h-0 shrink flex-col px-3 pt-2 [&>:first-child]:min-h-0" data-testid="chat-embed-question">
      <QuestionCard
        key={identity}
        questions={pending.questions}
        askId={pending.ask_id}
        busy={busy}
        draftAnswers={drafts.current.get(identity)}
        onDraftChange={(answers) => { drafts.current.set(identity, answers) }}
        onDismiss={() => answer(undefined)}
        onSubmit={(answers) => answer(answers)}
      />
      {/* No hand-off: the card above holds the selected answers. */}
      <ErrorNotice
        className="mt-2"
        message={notice}
        onDismiss={() => setFailure(null)}
        testId="chat-embed-question-error"
      />
    </div>
    </>
  )
}
