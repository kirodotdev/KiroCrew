/**
 * The only module of the XR page that changes anything on the gateway.
 *
 * Three writes exist: send a message to a Bot's own thread (opening that
 * thread first when the Bot was never talked to), approve one request, and
 * reject one request. Each uses the dashboard's own API client, so the
 * gateway's sign-in, origin and CSRF checks apply unchanged. `performWrite`
 * is reached only through `tryWrite` in `flags.ts`, and that only from
 * `PressRouter`'s `write` hook: a guarded press that the guard committed.
 *
 * The room of pages reaches one named write for each thing of its own, and
 * nothing else of this module: the board's stamp and its no (`boardDecide`,
 * from `workspace/useBoards.ts`), a question board's answer (`boardAnswer`,
 * from the same), and the send of words to a Bot (`cardSend`, from the new
 * chat card, `workspace/useChatCard.ts`, from a comment on a board,
 * `workspace/useComment.ts`, and from the readout's numbers tile,
 * `workspace/SendNumbersTile.tsx`), and the "Try again" of a failed sheet
 * (`cutRetry`, from `workspace/useBoards.ts`). All are on in the default
 * build, as a press on a page's own button is.
 */
import { api } from '../../api/client'
import { sendIdempotencyKey } from '../../api/sendRace'
import type { SharedDecision } from '../../api/sharedCrew'
import { alreadyResolvedDecision, decisionVerdict } from '../../components/chat-input/sharedDecision'
import type { ApprovalTarget } from '../../types'
import { i18nT } from '../../i18n/t'
import { toApiDecision } from '../../utils/approvalDecision'
import type { XrNeed } from './model'

export type XrWritePayload =
  | { kind: 'approve' | 'reject'; need: XrNeed }
  | { kind: 'send'; bot: string; slot: string; text: string }

/** The one request an approval or a rejection names. It never covers more than that. */
export function targetOf(need: XrNeed): ApprovalTarget {
  return need.origin === 'coordinator'
    ? { origin: 'coordinator', id: need.requestId, slot: need.slot, instance: need.requestMid }
    : { origin: 'native', id: need.requestId, slot: need.slot, mid: need.requestMid }
}

/** "This once" only: XR approves one call or rejects one call. It records no standing trust. */
const DECISION = { approve: 'approved', reject: 'rejected_once' } as const

/**
 * The board's answer (`workspace/board.ts`): a stamp approves, a throw with no stamp rejects, the one request of
 * `need`, this once, with `responseId`, which a retry reuses, so a retry is never a second answer. On in the default
 * build: it is the same single decision as the page's Allow once, and a stamp approves as a tap on it does on the
 * dashboard. Every other write still goes through `tryWrite`.
 */
export async function boardDecide(need: XrNeed, kind: 'approve' | 'reject', responseId: string): Promise<'approved' | 'rejected'> {
  if (need.kind !== 'approval' || !need.requestId || !responseId) throw new Error('not an approval')
  // What the request was decided as is what the gateway says: a replay (200) or a lost race (409) names the decision
  // that settled it, which may be another answer than this one.
  try {
    const reply = (await api.decideApproval(targetOf(need), toApiDecision(DECISION[kind]), responseId)) as { decision?: SharedDecision } | null
    return reply?.decision?.outcome ? decisionVerdict(reply.decision) : kind === 'approve' ? 'approved' : 'rejected'
  } catch (err) {
    const won = alreadyResolvedDecision(err)
    if (won) return decisionVerdict(won)
    throw err
  }
}

/**
 * How a question's wait had already ended, read from the gateway's refusal of an answer (404 `question_not_found`
 * with the reason it recorded): `answered` (also the answer of a retry whose first reply was lost), `dismissed`, or
 * `ended` (it expired, was withdrawn, or the reason is not known). `null` for any other failure.
 */
export function endedAsk(err: unknown): 'answered' | 'dismissed' | 'ended' | null {
  if (!err || typeof err !== 'object') return null
  const e = err as { status?: unknown; body?: unknown }
  if (e.status !== 404 || typeof e.body !== 'string') return null
  try {
    const body = JSON.parse(e.body) as { code?: unknown; reason?: unknown }
    if (body.code !== 'question_not_found') return null
    return body.reason === 'answered' ? 'answered' : body.reason === 'dismissed' ? 'dismissed' : 'ended'
  } catch {
    return null
  }
}

/**
 * A question board's answer (`workspace/question.ts`): `answers` keyed by each question's own words, as the dashboard's
 * question card sends them, or `null` to dismiss it (a board thrown with no print). Only for a question the gateway
 * waits on (`askId`), once: the gateway ends the wait at the first answer. It carries his own `responseId`, so the
 * gateway replays his retry and names another person's answer: an answer or a dismissal that someone else gave
 * first is `ended` (nothing of his went; "answered elsewhere"). A wait that had already ended with no decision to
 * name is no failure either: answered counts as answered, dismissed as the no, and any other end is `ended`.
 */
export async function boardAnswer(need: XrNeed, answers: Record<string, string> | null, responseId = ''): Promise<'approved' | 'rejected' | 'ended'> {
  if (need.kind !== 'question' || !need.askId) throw new Error('not a question')
  try {
    await api.answerQuestion(need.askId, answers ?? undefined, undefined, responseId || undefined)
  } catch (err) {
    if (alreadyResolvedDecision(err)) return 'ended'
    const was = endedAsk(err)
    if (!was) throw err
    return was === 'answered' ? 'approved' : was === 'dismissed' ? 'rejected' : 'ended'
  }
  return answers ? 'approved' : 'rejected'
}

/**
 * Words sent to a Bot from XR carry one added line that asks for a first answer in one short sentence (D26 (2)): what
 * he reads in XR is a sheet, and the shorter the Bot's first answer, the less he reads.
 */
export const xrMessage = (words: string): string => [words, i18nT('xr.first_short')].join(PARAGRAPH)
/** Between two paragraphs of a message. */
const PARAGRAPH = '\n\n'

/**
 * The whole read of a Bot's own Dashboard values, for its 3D dashboard in the room of pages (`workspace/useDash3d.ts`).
 * It is a POST, so it is named here, but it is a read: the Dashboard tab's own "give me every value again", gated
 * to the owner by the gateway, which answers from the live page it holds for that Bot and package. It changes
 * nothing of the Bot, its page or its values; on the gateway it only reads the Bot's folds again into that live
 * page's cache. Nothing else of the room may call the route.
 */
/**
 * "Try again" on a failed sheet (P3 slice 2, D35): the cut turn of that one session is picked up again, as the
 * dashboard's own Resume does (`api.continueSlot`). Called once for each print; a reply that does not come is not
 * sent again.
 */
export async function cutRetry(slot: string): Promise<void> {
  if (!slot) throw new Error('no session')
  await api.continueSlot(slot)
}

export function dashRead(slug: string, name: string, dashboard: string): Promise<{ dashboard?: string; read: { [key: string]: unknown } } | null> {
  return api.memberDashboardRefetch(slug, name, dashboard)
}

export async function performWrite(p: XrWritePayload): Promise<void> {
  if (p.kind === 'send') {
    if (!p.bot || !p.text.trim()) throw new Error('nothing to send')
    // A Bot that was never talked to has no thread yet. Opening it is part of the first send.
    const slot = p.slot || (await api.memberThread(p.bot)).slot_key
    if (!slot) throw new Error('no thread')
    // This call hands back the raw response: a refusal resolves, so it is checked here.
    const r = await api.sendChat(p.text, slot)
    if (!r.ok) throw new Error(`send refused (${r.status})`)
    return
  }
  if (p.need.kind !== 'approval' || !p.need.requestId) throw new Error('not an approval')
  await api.decideApproval(targetOf(p.need), toApiDecision(DECISION[p.kind]))
}

/** Where a card's words go: to one Bot's own thread (`thread` is '' when that Bot was never talked to), or into a new chat. */
export type CardSendTo = { bot: string; thread: string } | 'new'

/** One card's send: what it is, the thread it goes to once that is known, the request under way, and whether it went through. */
interface CardSend { to: string; text: string; slot: string; going: Promise<string> | null; done: boolean }
const cards = new Map<string, CardSend>()

/**
 * The send of words to a Bot or a new chat from XR (the new chat card, a comment on a board): `text` to `to`,
 * under the sender's own `id`, with the line of `xrMessage`. Answers the thread that the words went to.
 *
 * One id is one send. The id is the request's `Idempotency-Key`, so the gateway answers a repeated request once.
 * A call while the first is under way gets the first's own promise. A call after it went through gets its thread
 * and sends nothing. A call after it failed sends the same words to the same target again, to the same thread
 * (a new chat is started once for an id). A call under a used id with other words or another target is refused
 * here and sends nothing: the first request may have reached the gateway with only its reply lost, and the gateway
 * refuses a used key with another body for good.
 */
export function cardSend(text: string, to: CardSendTo, id: string): Promise<string> {
  const words = text.trim()
  if (!id || !words || (to !== 'new' && !to.bot)) return Promise.reject(new Error('nothing to send'))
  const target = to === 'new' ? 'new' : `bot:${to.bot}`
  const had = cards.get(id)
  if (had && (had.to !== target || had.text !== words)) return Promise.reject(new Error('this card was sent as something else'))
  if (had?.going) return had.going
  if (had?.done) return Promise.resolve(had.slot)
  const send: CardSend = had ?? { to: target, text: words, slot: '', going: null, done: false }
  cards.set(id, send)
  const run = async (): Promise<string> => {
    // A Bot that was never talked to has no thread yet. Opening it is part of the first send; so is the new chat's.
    const slot = send.slot || (to === 'new' ? (await api.createChatSlot()).key : to.thread || (await api.memberThread(to.bot)).slot_key)
    if (!slot) throw new Error('no thread')
    send.slot = slot
    // This call hands back the raw response: a refusal resolves, so it is checked here.
    const r = await api.sendChat(xrMessage(words), slot, undefined, undefined, undefined, undefined, undefined, { idempotencyKey: sendIdempotencyKey(id) })
    if (!r.ok) throw new Error(`send refused (${r.status})`)
    send.done = true
    return slot
  }
  // The request under way is forgotten when it ends, after this call has kept it: a later call then sends again.
  const going: Promise<string> = run().finally(() => { if (send.going === going) send.going = null })
  send.going = going
  return going
}

/**
 * The new chat that was started for a card whose send has not gone through, or ''. It stands in the list of chats,
 * and may hold the card's words (when only the gateway's reply was lost) or nothing: it is not deleted here, since
 * which of the two it is cannot be known.
 */
export function cardChatLeft(id: string): string {
  const send = cards.get(id)
  return send && !send.done && send.to === 'new' ? send.slot : ''
}
