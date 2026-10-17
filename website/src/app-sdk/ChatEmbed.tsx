/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux store: the
 * only chat-state imports are the pure queue-hydration helpers, so a queued
 * send renders here exactly as the dashboard's QueueStack shows it.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s). A poll that keeps
 * failing backs off to at most 30s, and once it has kept failing for
 * EMBED_STALL_MS (counted from its first failure) the page on screen is
 * flagged as stale instead of still reading as running.
 *
 * The poll is BOUNDED (chat-core P5-e): it asks the slot-detail endpoint for
 * the newest `EMBED_PAGE_LIMIT` rows, and the transcript's "load earlier" bar
 * widens that window by a page per press, up to the handler's own ceiling. An
 * unbounded poll re-read a whole 10 MB thread every second while it ran; the
 * rows are virtualized too, so the DOM cost no longer grows with history
 * either. Each poll is incremental (chat-core P5-f): it echoes the cursor the
 * last read returned and gets back only the rows after it, or the whole page
 * when anything earlier changed (`embedIncrementalRead`).
 *
 * Sending goes through the chat-core transport (`sendTurn`) over the app-sdk
 * wire, so the receipt contract is the shared one and the host app's
 * `allowedApiPaths` grant for `/api/chat` still gates the POST.
 *
 * The composer is the chat-core `Composer` root with its Editor and Send atoms
 * (`chat-core/composer/atoms.tsx`), both store-free; Send queues while a turn
 * runs. The root's Voice atom is off for this surface (`EMBED_CAPABILITIES`).
 */
import { useRef, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Paperclip } from 'lucide-react'
import ChatMessageList, { type VirtualTranscriptHandle } from './ChatMessageList'
import ErrorNotice from '../components/ErrorNotice'
import { FilePreviewStrip } from '../components/chat-input/FilePreviewStrip'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { useEmbedAttachments } from './useEmbedAttachments'
import { ComposerAttach } from '../chat-core/composer/Attach'
import { JumpToBottomButton } from './ChatScrollChrome'
import FollowUpBar from '../components/FollowUpBar'
import ChatFooter from '../pages/chat/ChatFooter'
import { deriveFollowUpOptions } from './protocol'
import { useComposerDraft } from './useComposerDraft'
import { useStopPress, type StopPhase } from '../chat-core/stop/useStopPress'
import { sendEmbedStop } from './embedStopTransport'
import { StopControls } from '../components/chat-input/StopControls'
import { useStopEscapeHatch } from '../hooks/useStopEscapeHatch'
import { useAppApi, useAppInfo } from './index'
import { appApiSendWire } from './appSendWire'
import { sendTurn, mintSendId } from '../chat-core/transport/sendTurn'
import { Composer, type ComposerCapabilities } from '../chat-core/composer/Composer'
import { ComposerEditor, ComposerSend } from '../chat-core/composer/atoms'
import { sendFailureCopy, deliveryUnconfirmedCopy } from '../chat-core/transport/receiptCopy'
import { mergeRecoveredDraft } from '../utils/chatDrafts'
import { drainEmbedRecovery, stashEmbedRecovery, subscribeEmbedRecovery, type PendingEmbedRecovery } from './embedSendRecovery'
import { retireSendTail, type SendTail } from './embedSendTails'
import { embedReadPath, isEmbedCursor, mergeEmbedRead, type EmbedCursor } from './embedIncrementalRead'
import type { ChatMessage } from '../types'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import QueueStack, { SubagentDeliveryProgress, splitPaneMessages } from '../components/QueueStack'
import { hydrateQueuedBubbles, normalizeQueueEntries } from '../store/chatSlice'

import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'

/** The `meta` a send with attachments carries (see `onSend`). */
export interface ChatEmbedSendMeta {
  /** Non-image attachments; `[attached_file N]` in the message names `files[N-1]`. */
  files?: string[]
}

export interface ChatEmbedProps {
  slotKey: string
  agent?: string
  placeholder?: string
  /**
   * Chrome-less rendering: drop the outer border/rounding/background and the
   * title strip, and make the input row transparent with no top border. Lets a
   * host page (e.g. the Spec Builder builtin) embed the chat flush inside its
   * own card. Defaults to false — existing embeds are unchanged.
   */
  frameless?: boolean
  /**
   * Jump the scroll to the bottom instantly on the first render (instead of the
   * default smooth scroll), then stay pinned to the bottom as content grows —
   * unless the user scrolls up more than 40px, which releases the pin until they
   * return to the bottom. Defaults to false — existing embeds keep the smooth
   * scroll-into-view behavior.
   */
  startAtBottom?: boolean
  /**
   * Send handler. When supplied, the composer routes through it INSTEAD of
   * `POST /api/chat`.
   *
   * The generic endpoint keys off `slotKey` alone and will CREATE the slot if it
   * is missing, with no app ownership and no project — so a stale tab (its spec
   * deleted elsewhere) could resurrect an unscoped session in which approved
   * tools run from the gateway's own directory. A host app that owns its slots
   * passes its own endpoint here, which can carry the app's identity checks and
   * refuse a stale send. Omitted, behaviour is unchanged.
   *
   * A send with non-image attachments passes a second `meta` argument whose
   * `files` the message's `[attached_file N]` markers name; a host forwards it
   * as `meta` on its own chat POST. A send without attachments passes only the
   * message, as before.
   */
  onSend?: (message: string, meta?: ChatEmbedSendMeta) => Promise<unknown> | void
  /**
   * Content rendered in normal flow directly ABOVE the composer, inside the
   * embed's own column, so it always sits on top of the input regardless of the
   * composer's height. A host uses this for a docked quote / reference bar
   * instead of absolutely positioning one over the transcript with a brittle
   * fixed offset that breaks whenever the composer's height changes.
   */
  aboveComposer?: ReactNode
  /**
   * Cap for the composer's auto-grow, in px. The textarea grows with the draft
   * up to this height, then keeps it and scrolls. Defaults to the shared
   * `useComposerDraft` cap (240px), which suits a full-height page but not a
   * host that boxes the embed at a fixed height: there a maxed-out draft takes
   * most of the box and the transcript above it is squeezed to a few lines. A
   * fixed-height host passes a proportion of its own box here; the resting
   * (empty) size is unaffected. Omitted, behaviour is unchanged.
   */
  composerMaxHeight?: number
  /**
   * Open a workspace file path the transcript names (a tool row's file chip, a
   * path link in a reply). Handed straight to `ChatMessageList`. Omitted, the
   * file chip is hidden and path links stay plain, as before.
   */
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /**
   * Open a saved artifact by slug from a link in a reply. Handed straight to
   * `ChatMessageList`. Omitted, artifact links follow their href as before.
   */
  onArtifactOpen?: (slug: string) => void
}

/** Stable empty transcript. A fresh `[]` fallback would be a new identity on every
 *  render, so `deriveFollowUpOptions` below would re-run (and hand FollowUpBar a new
 *  options array) on every render of an embed whose poll has not answered yet. */
const EMPTY_MESSAGES: ChatMessage[] = []

/** The embed's composer preset. Voice is off: its STT config read uses the
 *  dashboard api client, not this app's scoped api, so it would sidestep the
 *  host's `allowedApiPaths`, and its setup modal needs the dashboard router. */
const EMBED_CAPABILITIES: ComposerCapabilities = { voice: false }

/** Each send's transcript-tail record is `SendTail` (embedSendTails.ts): the
 *  row to show and `sendId`, the client-minted id stamped on the wire
 *  (`meta.sendId`, the same convention ChatPage and ChatPane use), so proof of
 *  delivery can be recognised as THIS message's own user row appearing -- by
 *  identity ONLY, never by text, which a manual resend or a duplicate
 *  injection could share. The server keeps the id on every path a send can
 *  take: a dispatched send persists it on its row directly, and a send that
 *  was queued behind a busy slot carries it on the queue entry so the drain
 *  writes it onto the row (#8853). Records are kept per send and retired
 *  independently, so one late send's proof can never be masked by another's
 *  recovery landing after it. */

/** Whether a polled user row is THIS send's own, by identity. A plain row
 *  carries `meta.sendId`; a row the drain merged from several queued sends
 *  (`merge_queued_messages`) names every send it stands for in `meta.sendIds`,
 *  and membership there is the same proof. Anything else -- no id, another
 *  id, an older backend that drops `meta` -- is not. */
function isOwnUserRow(m: ChatMessage, sendId: string): boolean {
  if (m.role !== 'user') return false
  const meta = m.meta
  if (!meta) return false
  if (meta.sendId === sendId) return true
  const many = meta.sendIds
  return Array.isArray(many) && many.includes(sendId)
}

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  /** A stop is in flight (the slot's `_stopping`). */
  stopping?: boolean
  /** The stop machine's phase; drives Stop's escalation. */
  stop_state?: StopPhase
  /** A Stop was declined for a compaction moments ago: the next press resets the session. */
  stop_declined?: boolean
  /** The running (or last ended) turn's number; absent before a turn is numbered. */
  turn?: number
  /** The gateway process's generation; with `turn`, names one turn across restarts. */
  turn_gen?: string
  title?: string
  /** Older rows exist beyond the bounded page. */
  has_more?: boolean
  /** Absolute index of the page's first row. */
  next_before?: number
  /** What the next poll echoes to get only the rows after it. */
  cursor?: EmbedCursor
  /** Present when this read carried only the rows after the echoed cursor. */
  delta?: { from: number; since: number }
  /** Messages waiting for the running turn: sent from this embed while busy,
   *  or from another surface. Rendered read-only above the composer. */
  queue?: unknown[]
}

/** Whether the raw slot queue holds the entry with this `queue_id` -- the id a
 *  `queued` receipt names. The wire form of a queue entry carries no
 *  `sendId`, so the receipt's id is how the embed recognises its own entry. */
function queueHasEntry(raw: unknown, queueId: string | undefined): boolean {
  if (!queueId || !Array.isArray(raw)) return false
  return raw.some(q => typeof q === 'object' && q != null && (q as { id?: unknown }).id === queueId)
}

/** One optimistic send: shown at once and held until the poll shows the
 *  server's copy (see `isPendingAcked`). */
interface PendingSend {
  slot: string
  sendId: string
  row: ChatMessage
  /** Shown as a queued card (the slot was busy, or the receipt said queued). */
  asQueued: boolean
  /** The `queue_id` a `queued` receipt named. */
  queueId?: string
  /** The server's copy has been seen; the optimistic copy stays hidden. */
  seen?: boolean
  viaHost: boolean
  text: string
  before: ReadonlySet<string>
}

/** Identity of a polled user row, for the host-`onSend` fallback below. */
const userRowKey = (m: ChatMessage) => (m.meta?.mid as string | undefined) ?? m.ts ?? m.content

/** Whether the poll shows THIS optimistic send. The default path stamps
 *  `sendId` on the wire, so identity is the proof. A host `onSend` posts on
 *  its own endpoint and no `sendId` comes back, so its fallback is a user row
 *  with the same text that was not in the transcript when the send went out. */
function isPendingAcked(
  p: { sendId: string; queueId?: string; viaHost: boolean; text: string; before: ReadonlySet<string> },
  messages: ChatMessage[],
  queue: unknown,
): boolean {
  if (messages.some(m => isOwnUserRow(m, p.sendId)) || queueHasEntry(queue, p.queueId)) return true
  return p.viaHost && messages.some(m => m.role === 'user' && m.content === p.text && !p.before.has(userRowKey(m)))
}

/** Rows per page of the bounded poll — the slot-detail handler's own default,
 *  so an embed asks for exactly what an unqualified read would have returned
 *  had it been bounded. Exported for tests. */
export const EMBED_PAGE_LIMIT = 200
/** The handler clamps `limit` here; a wider ask is silently this. */
export const EMBED_PAGE_LIMIT_MAX = 500
/** Poll cadence while the slot runs (see the query's `refetchInterval`). */
const RUNNING_POLL_MS = 1000
/** How long a streaming reply must stay unchanged before the working indicator
 *  takes over from the reply's own caret. The tail only grows once per poll,
 *  so the window spans two polls: one slow read must not flash the indicator
 *  under a reply that is still arriving. */
export const EMBED_STREAM_IDLE_MS = RUNNING_POLL_MS * 2 + 500
/** How long the poll may keep failing, measured from the first failure of the
 *  current run of failed reads (see `embedOutageMs`), before the page on screen
 *  is reported as stale. Shorter outages stay silent so a single dropped poll
 *  does not flash a notice. Exported for tests. */
export const EMBED_STALL_MS = 10_000
/** Ceiling of the failing poll's back-off. Exported for tests. */
export const EMBED_STALL_POLL_MAX_MS = 30_000

interface PollState {
  status: string
  data?: { running?: boolean }
  dataUpdatedAt: number
  errorUpdatedAt: number
}

/** Where the current run of failed reads began, per query key. Keyed on the
 *  last success it follows, so a success starts a fresh run. */
type FailureRuns = Map<string, { after: number; since: number }>

/** How long the current run of failed reads has lasted, in ms; 0 while the
 *  read is healthy. Measured from the run's FIRST failure, not from the last
 *  success: a key that never succeeded has no success time at all, and a tab
 *  that was hidden (the poll pauses there) carries a success time from before
 *  it was hidden, so either would read one dropped request as a long outage.
 *  Exported for tests. */
export function embedOutageMs(runs: FailureRuns, key: string, state: PollState): number {
  if (state.status !== 'error') {
    runs.delete(key)
    return 0
  }
  let run = runs.get(key)
  if (!run || run.after !== state.dataUpdatedAt) {
    run = { after: state.dataUpdatedAt, since: state.errorUpdatedAt }
    runs.set(key, run)
  }
  return state.errorUpdatedAt - run.since
}

/** Next poll delay. A healthy poll runs at the running/idle cadence. A failing
 *  one keeps that cadence until the outage reaches EMBED_STALL_MS (so the stall
 *  is noticed on time), then waits as long as the outage has lasted, up to the
 *  ceiling, so a dead endpoint is not hit every second forever. Exported for
 *  tests. */
export function embedPollInterval(state: PollState, outage: number): number {
  const base = state.data?.running ? RUNNING_POLL_MS : 5000
  if (state.status !== 'error' || outage < EMBED_STALL_MS) return base
  return Math.min(EMBED_STALL_POLL_MAX_MS, Math.max(base, outage))
}

function ChatEmbed({
  slotKey,
  agent,
  placeholder,
  frameless,
  startAtBottom,
  onSend,
  aboveComposer,
  composerMaxHeight,
  onFileOpen,
  onArtifactOpen,
}: ChatEmbedProps) {
  const api = useAppApi()
  // The parking scope for receipts that outlive this instance: this app's
  // embeds for this slot (see embedSendRecovery).
  const appName = useAppInfo().name
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const lastHashRef = useRef('')
  // The transcript is ChatMessageList's virtualized mount: it owns the scroller
  // and, in startAtBottom mode, the stick-to-bottom follow (the same
  // FollowController semantics as ChatPane and the main chat — re-pin on growth
  // AND collapse, released only by a genuine user scroll up). A top-anchored
  // embed opens at the top and is never pinned; it keeps its own contract
  // below — a deliberate smooth scroll to each NEW MESSAGE regardless of
  // position.
  const listRef = useRef<VirtualTranscriptHandle | null>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)
  const scrollToBottom = useCallback(() => { listRef.current?.scrollToBottom() }, [])

  // The bounded window: newest `limit` rows. "Load earlier" widens it by a
  // page. The widening is remembered against the slot it was made for, so a
  // new slot is back at one page on its very first read — no effect-timed
  // reset that would let one wide read of the new slot slip out first.
  const [widened, setWidened] = useState<{ slot: string; limit: number } | null>(null)
  const limit = widened?.slot === slotKey ? widened.limit : EMBED_PAGE_LIMIT

  // An embed is as long-lived as a ChatPane, so a one-shot read would leave it
  // on the old size after the user changes the setting elsewhere (ChatPane.tsx
  // follows the same `mc-config-changed`/`focus` reload).
  const [messageFontSize, setMessageFontSize] = useState(() => loadChatConfig().messageFontSize)
  useEffect(() => {
    const reload = () => setMessageFontSize(loadChatConfig().messageFontSize)
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  // One failure-run record per query key, shared by the poll cadence and the
  // stall notice so both measure the same outage.
  const failureRunsRef = useRef<FailureRuns>(new Map())
  const queryClient = useQueryClient()
  const { data: slotData, refetch, isPlaceholderData, isError, isFetching, status, dataUpdatedAt, errorUpdatedAt } = useQuery({
    queryKey: ['app-sdk-embed', slotKey, limit],
    // Incremental: the held page's cursor is echoed, and only the rows after
    // it come back while the rest still render the same (see
    // embedIncrementalRead). A delta that does not fit is read again whole.
    queryFn: async ({ queryKey }) => {
      const [, key, lim] = queryKey as [string, string, number]
      const held = queryClient.getQueryData<ChatSlotData>(queryKey)
      const sent = isEmbedCursor(held?.cursor) ? held.cursor : null
      const res = await api.get<ChatSlotData>(embedReadPath(key, lim, sent))
      return mergeEmbedRead(held, sent, res) ?? api.get<ChatSlotData>(embedReadPath(key, lim, null))
    },
    // A wider page replaces the narrower one on arrival; until then the rows
    // already on screen stay put instead of blinking through an empty list.
    // Same slot only: a slot change must not paint the previous slot's rows
    // under the new slot's header while its first read is in flight.
    placeholderData: (prev, prevQuery) =>
      prevQuery && (prevQuery.queryKey as unknown[])[1] === slotKey ? prev : undefined,
    refetchInterval: (query) => embedPollInterval(
      query.state,
      embedOutageMs(failureRunsRef.current, `${String(query.queryKey[1])}:${String(query.queryKey[2])}`, query.state),
    ),
  })

  // The last SETTLED page for this slot. A placeholder covers the in-flight
  // window of a widen, but a REJECTED widen leaves the wider key with no data
  // at all — and the transcript must not blank on a failed history fetch. The
  // settled page stays on screen and the bar shows the failure with a retry.
  const settledRef = useRef<{ slot: string; data: ChatSlotData } | null>(null)
  if (slotData && !isPlaceholderData) settledRef.current = { slot: slotKey, data: slotData }
  const settled = settledRef.current?.slot === slotKey ? settledRef.current.data : undefined
  const shown = slotData ?? settled

  // The widen failed: the wider read errored and the rows on screen are still
  // the narrower page. An ambient poll error on a settled page (nothing being
  // widened) is not the bar's to report.
  const widenFailed = isError && limit > EMBED_PAGE_LIMIT && slotData == null && settled != null
  // The poll on the page shown has kept failing for EMBED_STALL_MS, counted
  // from the first failure of the run (gateway restart, deleted session, lost
  // permission). The page is then a stale snapshot: its `running` says nothing
  // about now, so it is not shown as running, and the notice above the
  // composer says so.
  const outage = embedOutageMs(failureRunsRef.current, `${slotKey}:${limit}`, { status, data: slotData, dataUpdatedAt, errorUpdatedAt })
  const stalled = isError && slotData != null && !widenFailed && outage >= EMBED_STALL_MS
  // Reconnect is the same read the poll makes, issued now. While a read is in
  // flight the button is busy, so a click visibly does something; a click
  // whose read fails remounts the notice so its alert is announced again
  // instead of the screen staying exactly as it was.
  const [reconnectFailures, setReconnectFailures] = useState(0)
  const reconnect = useCallback(async () => {
    const result = await refetch()
    if (result.isError) setReconnectFailures((n) => n + 1)
  }, [refetch])

  const serverMessages = shown?.messages ?? EMPTY_MESSAGES

  const running = !stalled && (shown?.running ?? false)
  // A stale page's half-written reply must not keep its live caret: render
  // the tail as a settled row while the poll is stalled.
  const listMessages = useMemo(() => {
    const last = serverMessages[serverMessages.length - 1]
    if (!stalled || last?.role !== 'streaming') return serverMessages
    return [...serverMessages.slice(0, -1), { ...last, role: 'assistant' }]
  }, [serverMessages, stalled])
  const title = shown?.title ?? ''
  // No page yet for this slot and no failure: the title is still unknown.
  const titlePending = shown == null && !isError
  // Only a loaded page with no history, here or beyond the page, is new.
  const isFresh = shown != null && serverMessages.length === 0 && !shown.has_more
  const headerTitle = title
    || (titlePending ? i18nT('appSdk.chatEmbed.loading_session_title')
      : isFresh ? i18nT('appSdk.chatEmbed.untitled_session_title')
        : i18nT('appSdk.chatEmbed.untitled_existing_session_title'))
  // Queued sends, through the same hydration and split the dashboard panes
  // use, so a sub-agent delivery or a recovery continuation never shows as a
  // user card here either. Read-only: the embed offers no cancel/edit, so it
  // adds no write path; the cards retire when the drain lands the row.
  const queueRaw = shown?.queue

  // The just-sent message, shown at once. Without it the composer cleared and
  // the message was missing until the next poll — up to 5 s at the idle
  // cadence. It is shown where the server's copy will appear, so it never
  // jumps: an idle slot gets a user row at the transcript's end, and a slot
  // that is busy at send time gets a queued card in the queue strip (the send
  // will queue behind the running turn). It is held until the poll shows THIS
  // send: its own user row by `sendId` (`isOwnUserRow`), or its queue entry
  // by the `queue_id` a `queued` receipt names; that copy replaces it. Waiting on the poll rather than on the send
  // settling keeps it correct whether the receipt comes back before or after
  // the row is persisted. A send that fails or cannot be confirmed drops it
  // (`deliver` below): its text goes back to the composer, and two copies
  // would invite a duplicate resend.
  // One record PER SEND, oldest first: a second send made before the poll
  // shows the first must not erase the first's copy.
  const [pending, setPending] = useState<PendingSend[]>([])
  const pendingHere = useMemo(() => pending.filter(p => p.slot === slotKey), [pending, slotKey])
  // Latched: once the server's copy has been seen, the optimistic copy stays
  // hidden even if that copy later leaves the poll (a queue entry cancelled
  // from another surface, a row scrolled out of the bounded page) -- else it
  // would come back as a message that is not there.
  const ackedNowIds = useMemo(
    () => pendingHere.filter(p => !p.seen && isPendingAcked(p, serverMessages, queueRaw)).map(p => p.sendId),
    [pendingHere, serverMessages, queueRaw],
  )
  const ackedKey = ackedNowIds.join(',')
  useEffect(() => {
    if (!ackedKey) return
    const ids = new Set(ackedKey.split(','))
    setPending(list => list.map(p => (ids.has(p.sendId) ? { ...p, seen: true } : p)))
  }, [ackedKey])
  // A seen record is kept only while its server queue entry is still on
  // screen, so that entry can keep the card's key; after that it is done.
  const queueIds = useMemo(
    () => new Set((Array.isArray(queueRaw) ? queueRaw : [])
      .map(q => (q as { id?: unknown } | null)?.id)
      .filter((x): x is string => typeof x === 'string')),
    [queueRaw],
  )
  const doneKey = pendingHere
    .filter(p => p.seen && !(p.asQueued && p.queueId != null && queueIds.has(p.queueId)))
    .map(p => p.sendId).join(',')
  useEffect(() => {
    if (!doneKey) return
    const ids = new Set(doneKey.split(','))
    setPending(list => list.filter(p => !ids.has(p.sendId)))
  }, [doneKey])
  const isShown = useCallback(
    (p: PendingSend) => !p.seen && !ackedNowIds.includes(p.sendId),
    [ackedNowIds],
  )
  const messages = useMemo(() => {
    const rows = pendingHere.filter(p => !p.asQueued && isShown(p)).map(p => p.row)
    return rows.length ? [...listMessages, ...rows] : listMessages
  }, [listMessages, pendingHere, isShown])
  const { queuedMessages, systemDeliveryCount } = useMemo(() => {
    const entries = normalizeQueueEntries(queueRaw)
    for (const p of pendingHere) {
      if (!p.asQueued) continue
      // One card for this send from first paint to drain: the server's entry,
      // once it arrives, takes over the optimistic card's key (QueueStack keys
      // by `queueId`), so the card stays put instead of animating out while a
      // second one animates in.
      const key = `optimistic-${p.sendId}`
      const i = p.queueId ? entries.findIndex(e => e.queueId === p.queueId) : -1
      if (i >= 0) entries[i] = { ...entries[i], queueId: key }
      else if (isShown(p)) entries.push({ content: p.text, queueId: key, ts: p.row.ts ?? '' })
    }
    return splitPaneMessages(hydrateQueuedBubbles([], entries))
  }, [queueRaw, pendingHere, isShown])
  // No page at all and the read failed: the transcript is unavailable, which is
  // not the same thing as an empty session — say so, with the retry.
  const loadFailed = isError && shown == null
  const widening = isPlaceholderData && !isError
  const canWiden = ((shown?.has_more ?? false) && limit < EMBED_PAGE_LIMIT_MAX) || widenFailed
  const widen = useCallback(() => setWidened((w) => {
    const current = w?.slot === slotKey ? w.limit : EMBED_PAGE_LIMIT
    return { slot: slotKey, limit: Math.min(current + EMBED_PAGE_LIMIT, EMBED_PAGE_LIMIT_MAX) }
  }), [slotKey])
  // Retry re-issues the read at the limit that failed rather than widening again.
  const retryWiden = useCallback(() => { void refetch() }, [refetch])

  /** Derived from the same helper the main chat and side panel use, so "options only
   *  after the answer settles" and "a later user message clears them" behave identically
   *  here too — an agent's follow-up choices should never be silently dropped just
   *  because the surface embedding them is thinner. */
  const { followUpOptions, followUpMulti, followUpSourceKey } = useMemo(
    () => deriveFollowUpOptions(messages, running),
    [messages, running]
  )

  /** The composer's draft behaviour, owned by the chat SDK rather than by this file —
   *  see useComposerDraft's own docs. Picking a follow-up option edits the draft
   *  (matching every other surface) instead of sending immediately. */
  const { draft, setDraft, textareaRef, picked, toggleOption, composition, submitOnEnter } =
    useComposerDraft({ followUpOptions, multi: followUpMulti, sourceKey: followUpSourceKey, maxHeight: composerMaxHeight })

  /** What recent sends left behind on the transcript's tail, if anything:
   *  an `error` row for a send that never went out (the same role ChatPage and
   *  ChatPane append into their slot), or a `notice` row for one whose
   *  delivery could not be confirmed. This embed owns no store, and the slot
   *  detail it polls has no record of a message the server never took, so the
   *  rows live here -- and a receipt that lands after this instance unmounted
   *  is parked by slot (`embedSendRecovery`) for the next instance to show.
   *  One record PER SEND, oldest first: a drain can apply several at once, and
   *  each must keep its own id so its own proof of delivery can retire it
   *  (below). Cleared when the next COMPOSER submit starts -- that submit
   *  consumes the draft the tails' restores wrote into, so the user has
   *  decided what to do with the handed-back text. An override send (a
   *  follow-up chip's own text) consumes nothing: its tails stay, so a late
   *  send's proof can still retire its notice and take the restored copy
   *  back instead of stranding it in the composer for a duplicate resend. */
  const [sendTails, setSendTails] = useState<SendTail[]>([])
  const attachments = useEmbedAttachments(api, slotKey)
  const { restore: restoreFiles, removeFile } = attachments
  const shownMessages = useMemo<ChatMessage[]>(
    () => sendTails.length === 0 ? messages : [...messages, ...sendTails.map(t => ({ role: t.role, content: t.content, cls: '' }))],
    [messages, sendTails],
  )
  /** Latest rendered draft, for the async send path to read at settle time
   *  instead of a value closed over at render. */
  const draftRef = useRef(draft)
  draftRef.current = draft
  // An "unconfirmed" notice answers the question "did it go out?". Proof is a
  // USER row for THIS send anywhere in the polled window, recognised by
  // identity ONLY: a row carrying this send's `meta.sendId` (or naming it in
  // a merged row's `meta.sendIds`). No positional gate: the poll is BOUNDED
  // (newest `EMBED_PAGE_LIMIT` rows, widened by "load earlier"), so the
  // window's length neither grows with a delivered send once the transcript
  // is past the limit nor stays put when a page is prepended -- only the id
  // is a stable signal, and it is sufficient because nothing else can carry
  // it. The server keeps the id on both paths a send can take -- persisted
  // directly on a dispatched send's row, carried through the queue entry onto the drained
  // row when the slot was busy (#8853) -- so a same-text resend or injection
  // can never be mistaken for it, and there is no text-matching fallback to
  // false-retire on. Only that proof retires the notice and takes back the
  // text it handed back -- and only if the composer still holds exactly what
  // the restore produced; anything typed or edited since stays. Transcript
  // growth from anything else (a prior turn still streaming, a cron or
  // sub-agent injecting text into the slot, a same-text row under another id,
  // an older backend that drops `meta`) is NOT proof: the send may still be
  // undelivered, so the notice and the restored text both stay -- withdrawing
  // the text on unproven delivery would re-create the silent loss this surface
  // is being fixed for. An `error` tail is never retired here: nothing
  // arriving later makes a refused send less refused. Each notice is judged
  // against ITS OWN id and retired on its own -- two late sends drained
  // together stay two records, so proof of the first cannot be masked by the
  // second's recovery having landed after it, and taking the first's text back
  // rebuilds the composer around the second's (`retireSendTail`).
  useEffect(() => {
    const proven = sendTails.filter(t =>
      t.role === 'notice'
      && messages.some(m => isOwnUserRow(m, t.sendId)))
    if (proven.length === 0) return
    let tails = sendTails
    let draft = draftRef.current
    for (const t of proven) {
      const r = retireSendTail(tails, t.sendId, draft, mergeRecoveredDraft)
      tails = r.tails
      if (r.draft != null) draft = r.draft
      // The send's files were delivered with it: unstage the handed-back
      // copies still in the composer so they are not sent a second time.
      for (const path of t.restoreFiles ?? []) removeFile(path)
    }
    setSendTails(tails)
    if (draft !== draftRef.current) {
      draftRef.current = draft
      setDraft(draft)
    }
  }, [messages, sendTails, setDraft, removeFile])

  // startAtBottom follow is owned by the virtualizer behind ChatMessageList.
  // Non-startAtBottom embeds keep the message-arrival smooth scroll: it fires
  // on NEW MESSAGES only (not on content growth) and deliberately scrolls
  // regardless of position — a top-anchored embed announcing each reply.
  // Keyed on the TAIL row's identity, not the row count: a "load earlier"
  // press prepends a page, which must not read as a new reply and yank the
  // reader away from the history they just asked for. The send-tail rows (a
  // failed or unconfirmed send, above) count as new messages here: they are
  // the one row that must never land below the fold unannounced, or a failed
  // send looks sent again.
  const tail = messages[messages.length - 1]
  const msgHash = `${(tail?.meta?.mid as string | undefined) ?? tail?.ts ?? ''}:${tail?.content?.length ?? 0}`
    + ':' + sendTails.map(t => t.role + t.content.length).join(',')
  useEffect(() => {
    if (startAtBottom) return
    if (msgHash === lastHashRef.current) return
    lastHashRef.current = msgHash
    listRef.current?.scrollToBottom('smooth')
  }, [msgHash, startAtBottom])

  /** Hand an undelivered message back to the composer. The user may have typed
   *  more since the send cleared it; `mergeRecoveredDraft` owns the rule for
   *  keeping both, for every recovery site in the app. The composer is a
   *  `<textarea>`, so the rule's paragraph break and the user's own line
   *  breaks are kept exactly. Returns the composer
   *  value before and after, so an "unconfirmed" restore can be undone later
   *  if delivery is proven and the user has not touched it since. */
  const restoreIntoComposer = useCallback((text: string): { before: string; after: string } => {
    const before = draftRef.current
    const after = mergeRecoveredDraft(before, text)
    // Advance the ref eagerly: two restores in one tick (a drain of several
    // parked receipts) must each merge on top of the previous one, and the
    // render that would refresh the ref has not happened yet.
    draftRef.current = after
    setDraft(after)
    return { before, after }
  }, [setDraft])

  /** Show a receipt's outcome on THIS embed: its own tail record appended
   *  (never replacing another send's), and the handed-back text merged into
   *  the composer (with the before/after an unconfirmed restore needs so
   *  proven delivery can take it back). One function for the live path and
   *  for a receipt drained after a remount, so the two cannot render the same
   *  outcome differently. */
  const applyRecovery = useCallback((rec: PendingEmbedRecovery) => {
    const restore = rec.restoreText == null ? undefined : { text: rec.restoreText, ...restoreIntoComposer(rec.restoreText) }
    // Staged files ride back with the text, so a retry needs no new upload.
    if (rec.restoreFiles?.length) restoreFiles(rec.restoreFiles)
    setSendTails(prev => [...prev.filter(t => t.sendId !== rec.tail.sendId), { ...rec.tail, restore, restoreFiles: rec.restoreFiles }])
  }, [restoreIntoComposer, restoreFiles])

  /** Whether this instance is still mounted, and which slot it currently
   *  shows. A receipt is asynchronous: by the time it lands the host may have
   *  unmounted the embed (switched specs, closed a drawer) or re-propped it to
   *  another slot. Writing the recovery into a dead instance would lose the
   *  only copy of a refused send -- the server never took it, so no poll will
   *  ever show it -- and drop the "unconfirmed" warning for a late one. Such a
   *  receipt is parked by slot in `embedSendRecovery` and drained by the next
   *  embed mounted for that slot (below). */
  const mountedRef = useRef(true)
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  useEffect(() => {
    // Drain anything a previous instance parked for this slot -- every record,
    // oldest first, so two overlapping sends that both failed each get their
    // text back -- and keep listening: a receipt can land AFTER this instance
    // mounted (the user came back within the deadline) but be addressed by the
    // instance that sent it, which by then is unmounted and can only park it.
    // The subscription is keyed to the slot it was made for, and this instance
    // can be re-propped to another slot (or unmounted) before this effect's
    // cleanup runs: `slotKeyRef` advances during render, the unsubscribe only
    // on the passive flush. A receipt landing in that gap would fire the
    // still-live listener and drain slot A's only copy into the composer now
    // showing slot B, deleting it from A. So the drain re-checks, at call
    // time, that it is still the instance for THIS slot; otherwise the record
    // stays parked for the embed that actually shows the slot.
    const drain = () => {
      if (!mountedRef.current || slotKeyRef.current !== slotKey) return
      for (const rec of drainEmbedRecovery({ app: appName, slot: slotKey })) applyRecovery(rec)
    }
    drain()
    return subscribeEmbedRecovery({ app: appName, slot: slotKey }, drain)
  }, [appName, slotKey, applyRecovery])

  const wire = useMemo(() => appApiSendWire(api, agent), [api, agent])

  // Receipt semantics live in the chat-core transport: `sendTurn` owns the
  // abort deadline and the shared classification, reached here through the
  // app-sdk wire so the host app's `allowedApiPaths` grant still gates the
  // POST. This embed only decides how to REACT per status:
  // - `refused` / `transport-error`: nothing was sent. Say so on the
  //   transcript and hand the text back. The old path reported neither -- it
  //   swallowed the SSE stream's parse error as success and never consumed
  //   `sendMutation.isError`, so a failed send looked sent and the text was
  //   gone. (With the app-sdk wire, a rejected fetch is reported as
  //   `response-late`, not `transport-error`: one POST with no response cannot
  //   tell "never left" from "accepted, then the connection dropped", and the
  //   second means a resend runs the turn twice. The branch stays for the
  //   contract's sake; the wire does not currently produce it.)
  // - `response-late`: the deadline fired with no receipt either way. ChatPane
  //   leaves this alone because its optimistic bubble stays on screen; this
  //   embed drops its optimistic row (it has no failure state to put on it)
  //   and the composer has already cleared, so the user's text has NO
  //   visible copy left -- the case the transport contract
  //   names as the one a caller may recover. Hand the text back and say the
  //   delivery is unconfirmed (a notice, not a failure: the turn may well be
  //   running, and the poll will show it if so).
  // - `unknown`: a 2xx was received -- the server has the message and only
  //   the receipt was mangled. Restoring would invite a duplicate; do nothing.
  // - `dispatched` / `queued`: the server has the message. The optimistic row
  //   stays until the poll renders the server's copy by `sendId`.
  // A host-supplied `onSend` keeps its own endpoint and its rejection proves
  // nothing about delivery (a host may have posted and lost the answer), so
  // it is treated like `response-late`: unconfirmed, text handed back, never
  // reported as a failure.
  //
  // Only a composer submit has anything to hand back: an option send never
  // consumed the draft, so restoring the option label would CLOBBER a draft
  // the user can re-click the chip for any time (the same gate ChatPane and
  // ChatPage keep). The unconfirmed notice says so in its own words for a
  // chip send -- "tap the suggestion again" -- instead of claiming a restore that
  // did not happen.
  const sendMutation = useMutation({
    // `msg` is the trimmed wire text; `draftText` is what the composer held,
    // whitespace and all -- a recovery must give back what the user typed, not
    // what the wire carried.
    mutationFn: async ({ msg, meta, files, draftText, override, sendId, slot }: { msg: string; meta?: ChatEmbedSendMeta; files: string[]; draftText: string; override: boolean; sendId: string; slot: string }) => {
      // Where the outcome goes: onto this embed if it is still mounted and still
      // showing the slot the send belonged to; otherwise parked by that slot for
      // the next embed that shows it. Only a composer submit has text to hand
      // back -- a chip send never consumed the draft.
      const deliver = (tail: PendingEmbedRecovery['tail']) => {
        // The text is going back to the composer: drop this send's optimistic
        // row so the user never sees two copies of it.
        setPending(list => list.filter(p => p.sendId !== sendId))
        const rec: PendingEmbedRecovery = override
          ? { tail }
          : { tail, restoreText: draftText, ...(files.length ? { restoreFiles: files } : {}) }
        if (mountedRef.current && slotKeyRef.current === slot) applyRecovery(rec)
        else stashEmbedRecovery({ app: appName, slot }, rec)
      }
      // The copy is the core's (`receiptCopy.ts`, shared with SideChat): a
      // refusal FRAMED as a failed send rather than the server's bare sentence
      // -- the optimistic row is dropped first, leaving no bubble to anchor a raw reason to --
      // and "your text is back" only when the send consumed the draft (a chip
      // send never did).
      const fail = (reason?: string) => {
        deliver({ role: 'error', content: sendFailureCopy(reason, !override), sendId })
      }
      const unconfirmed = () => {
        deliver({ role: 'notice', content: deliveryUnconfirmedCopy(!override), sendId })
      }
      if (onSend) {
        try {
          await (meta ? onSend(msg, meta) : onSend(msg))
        } catch {
          unconfirmed()
        }
        return
      }
      const receipt = await sendTurn({ message: msg, slot, meta: { ...meta, sendId }, wire })
      // `transport-error` is not handled: the app-sdk wire never yields it (a raw
      // rejection is indeterminate -> `response-late`), so a clause for it here
      // would be dead code kept for symmetry.
      if (receipt.status === 'queued') {
        // Queued behind a running turn: the server's copy will be a queue
        // card, so the optimistic copy moves there too (it already is one when
        // the slot was busy at send time) and waits for that entry by id.
        const queueId = typeof receipt.body.queue_id === 'string' ? receipt.body.queue_id : undefined
        setPending(list => list.map(p => (p.sendId === sendId ? { ...p, asQueued: true, queueId } : p)))
      }
      if (receipt.status === 'refused') fail(receipt.reason)
      if (receipt.status === 'response-late') unconfirmed()
    },
    // A send that threw never reached the transcript: drop its optimistic row.
    onError: (_err, { sendId }) => { setPending(list => list.filter(p => p.sendId !== sendId)) },
    onSettled: () => { void refetch() },
  })

  /** `override` carries the text a follow-up chip's send arrow supplies (double-click
   *  or the send segment); without it the draft is the source of truth. Every call
   *  site wraps this in an arrow, so a click event can never arrive here as the
   *  override — mirrors SideChat's send(). Guarded on `sendMutation.isPending` so a
   *  chip's send arrow (unlike the composer's own Send button) can't fire a second
   *  turn before the first settles. Only a composer submit owns the composer's
   *  text — an override send carries its own text, so clearing the draft here would
   *  throw away a draft the user has not sent yet — and, for the same reason, only
   *  a composer submit clears the send tails: their restores live in that draft,
   *  and an override send that wiped them would leave a late-accepted send's
   *  restored text with no proof path left to retire it. */
  const send = useCallback((override?: string) => {
    if (sendMutation.isPending) return
    // Staged files belong to the composer, so only a composer submit takes them;
    // a follow-up chip's override send carries its own text alone.
    const files = override == null ? attachments.files : []
    // A composer submit (Enter or Send) waits for uploads still in flight, so
    // a file pasted just before Enter is not left behind for the next message.
    if (override == null && attachments.uploading) return
    const draftText = override ?? draft
    const text = draftText.trim()
    if (!text && !files.length) return
    if (override == null) {
      setSendTails([])
      setDraft('')
    }
    // A fresh id and the slot the send belongs to: the yardstick for "did the
    // poll show THIS send", and the address a receipt is delivered to if this
    // embed is gone by then.
    const sendId = mintSendId()
    // The same file wire shape as the main chat: image lines inline, other
    // files as `[attached_file N]` markers naming `meta.files`. The text-only
    // channel (`'steer'`), because this surface carries no `meta.dirs`: a
    // send would rewrite an `@src/utils/` folder token into a marker naming a
    // path nothing travels with, while a text-only send keeps the token as
    // typed -- exactly what the same text sent without a file carries.
    const turn = files.length ? buildOutgoingTurn({ text, files }, 'steer') : null
    const msg = turn ? turn.wire : text
    const meta = turn?.meta.files ? { files: turn.meta.files } : undefined
    if (turn) attachments.clear()
    // No `sendId` on the optimistic row's meta: it would read as this send's
    // own server row and falsely prove an unconfirmed notice delivered.
    const ts = new Date().toISOString()
    setPending(list => [...list, {
      slot: slotKey,
      sendId,
      row: { role: 'user', content: msg, cls: 'user', ts, meta: { clientTs: ts, optimistic: true } },
      asQueued: running,
      viaHost: onSend != null,
      text: msg,
      before: new Set(serverMessages.filter(m => m.role === 'user').map(userRowKey)),
    }])
    sendMutation.mutate({ msg, meta, files, draftText, override: override != null, sendId, slot: slotKey })
  }, [draft, setDraft, sendMutation, slotKey, onSend, serverMessages, running, attachments])

  const pickerLabel = attachments.uploading
    ? i18nT('appSdk.chatEmbed.uploading_files')
    : i18nT('components.chatInput.attach_files')

  // Stop sits where Send does while a turn runs and the draft is empty (a
  // draft mid-turn still sends, which queues it), as in the main composer.
  const refetchNow = useCallback(() => { void refetch() }, [refetch])
  const serverTurn = typeof shown?.turn === 'number' ? `${shown.turn_gen ?? ''}:${shown.turn}` : undefined
  // The main chat's press protocol (chat-core/stop/useStopPress) over the
  // embed's scoped API. The embed learns turns from a poll, so it scopes the
  // press state to the turn, and it reads the backend's compaction decline.
  const sendStop = useCallback(
    (force: boolean) => sendEmbedStop(api, slotKey, force).finally(refetchNow),
    [api, slotKey, refetchNow],
  )
  const { stopState, onStop, stopError, stopDeclined } = useStopPress({
    slotKey, busy: running, serverStopState: shown?.stop_state, sendStop,
    turnScoped: true, turnKey: serverTurn, serverStopDeclined: shown?.stop_declined === true,
  })
  // Staged files count as a draft, as in the main composer's
  // `composerHasDraft`: Stop must not take the place of sending them.
  const showStop = running && !draft.trim() && attachments.files.length === 0
  // Says what Stop does now, not "again": the warning also shows to a reader
  // who opened or returned to the embed after someone else's press.
  const stopResetHint = (
    <span className="text-[13px] leading-4 text-muted" data-testid="chat-embed-stop-hint">
      {i18nT('appSdk.chatEmbed.stop_restarts_session')}
    </span>
  )
  const { escaped: killingEscaped } = useStopEscapeHatch(stopState)
  // What the next press does, shown only while the Stop button is on screen
  // (a draft swaps it for Send, which queues). It shows beside a failure
  // notice too: a press that never got an answer keeps `soft_pending`, so the
  // next press is a force stop, and its cost must be said before it is made.
  const pressHint = !showStop ? null
    : stopDeclined ? stopResetHint
    : stopState === 'killing' && killingEscaped ? (
      // An instruction, not a status line: the same press re-sends the force stop.
      <span className="text-[13px] leading-4 text-muted" data-testid="chat-embed-stop-hint">
        <span className="text-text">{i18nT('components.chatInput.click_again_to_force_stop')}</span>
        {' · '}
        {i18nT('components.chatInput.taking_longer_than_expected')}
      </span>
    ) : stopState === 'soft_pending' ? (
      // The cost sits beside the instruction, where the user decides,
      // not only in the button's tooltip: force stop drops queued work.
      <span className="text-[13px] leading-4 text-muted" data-testid="chat-embed-stop-hint">
        <span className="text-text">{i18nT('components.chatInput.click_again_to_force_stop')}</span>
        {' · '}
        {i18nT('components.chatInput.force_kill_discards_in_progress_work_and_queued')}
      </span>
    ) : null
  // One status while a stop is under way: the header says "Stopping" with the
  // footer, instead of "streaming". The placeholder stays neutral, so a narrow
  // embed does not say it three times.
  const stopUnderWay = running && (stopState !== 'idle' || !!shown?.stopping)
  // The footer says "Killing…" once the force stop runs; the header and the
  // placeholder follow it rather than keep saying "Stopping…".
  const stopLabel = stopState === 'killing' ? i18nT('components.chatInput.killing') : i18nT('components.chatInput.stopping')

  // Resolve a pending tool approval from inside the embed.
  //
  // Without this the group header rendered a dead "Approval needed" label with
  // no buttons: ChatMessageList only shows the Approve/Reject controls when an
  // onApprove handler is supplied, and the embed supplied none. An embedded
  // agent that hit a permission prompt was therefore unactionable and blocked
  // until the runner's timeout auto-rejected it.
  //
  // Routed through the SLOT approval endpoint, which is the only one that can
  // express all three decisions. /api/approvals/{id}/{action} accepts just
  // approve|reject, so mapping 'trust' onto it silently downgraded a Trust click
  // to a one-shot approve: the card said "Trusted" and the very next tool call
  // prompted again. POST /api/chat/slots/{slot}/approve carries the decision
  // verbatim plus the request_id, so trust sets the owner slot's policy.
  //
  // Requires the host app to grant '/api/chat' in its allowedApiPaths.
  const approveMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) =>
      api.post(`/api/chat/slots/${encodeURIComponent(slotKey)}/approve`, {
        action: decision,
        request_id: id,
      }),
    onSettled: () => { void refetch() },
  })

  // mutateAsync, not mutate: the returned promise carries a failed POST to the
  // approval row's rollback (CollapsibleToolGroup.submitDecision catches it and
  // restores the buttons). mutate() returns void, so a failed POST would leave
  // the row optimistically resolved while the agent stays parked on the
  // undelivered decision, with no retry path.
  const approve = useCallback(
    (approvalId: string, decision: string) => approveMutation.mutateAsync({ id: approvalId, decision }),
    [approveMutation],
  )

  // Batch resolver (Req 4.1-4.4): apply one decision to every pending approval
  // in a group. Each id goes through the SAME slot-scoped approve endpoint the
  // single path uses (POST /api/chat/slots/{slot}/approve with request_id) —
  // Task 4 mandates the slot-scoped path for batches, never the bare id-scoped
  // one-shot resolve (which matches slot futures by bare id with no session
  // check). Uses allSettled, NOT a fail-fast loop: a call whose verdict changed
  // between surfacing and resume (Req 4.3-4.4) is surfaced as an excluded
  // rejection instead of aborting the batch with earlier ids already approved.
  // Rejects (so the row rolls back) only if EVERY call failed; a partial
  // success settles as resolved and refetch reconciles the still-pending rows.
  const approveBatch = useCallback(
    async (approvalIds: string[], decision: string) => {
      const results = await Promise.allSettled(
        approvalIds.map(id => api.post(`/api/chat/slots/${encodeURIComponent(slotKey)}/approve`, {
          action: decision,
          request_id: id,
        })),
      )
      void refetch()
      const rejected = results.filter(r => r.status === 'rejected')
      if (rejected.length === approvalIds.length) throw (rejected[0] as PromiseRejectedResult).reason
      return results
    },
    [api, slotKey, refetch],
  )

  return (
    <div
      className={`flex flex-col h-full min-h-0 overflow-hidden ${frameless ? '' : 'border border-border rounded-lg bg-bg'}`}
      style={{ '--mc-message-font-size': `${messageFontSize}px` } as React.CSSProperties}
    >
      {!frameless && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
          {/* Muted while stalled: the idle accent would say the session is fine.
              Danger while a stop is under way, so the dot agrees with the
              "Stopping…"/"Killing…" caption beside it and the footer below
              instead of staying green as if the turn were working normally. */}
          <span data-testid="chat-embed-status-dot" className={`w-2 h-2 rounded-full shrink-0 ${stalled ? 'bg-muted' : stopUnderWay ? 'bg-danger animate-pulse' : running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
          {/* An untitled session reads a localized label (loading, new, or
              untitled), never the internal slot key; the key stays reachable
              on hover for whoever is debugging. */}
          <span
            className={`text-[13px] font-semibold truncate flex-1 ${titlePending ? 'text-muted' : 'text-text-strong'}`}
            title={slotKey}
            aria-busy={titlePending || undefined}
            data-testid="chat-embed-title"
          >
            {headerTitle}
          </span>
          {agent && <span className="text-[10px] font-mono text-muted">{agent}</span>}
          {running && <span className={`text-[10px] font-mono ${stopUnderWay ? 'text-danger' : 'text-ok'}`}>{stopUnderWay ? stopLabel : i18nT('appSdk.chatEmbed.streaming')}</span>}
        </div>
      )}

      {/* canTrust: this embed's approve routes through the slot approve
          endpoint (above), which records standing trust — the one mount
          allowed to offer the tier (#5434). */}
      <ChatMessageList
        ref={listRef}
        messages={shownMessages}
        running={running}
        onApprove={approve}
        onApproveBatch={approveBatch}
        canTrust
        onFileOpen={onFileOpen}
        onArtifactOpen={onArtifactOpen}
        // No hand-off: the embed's composer draft is unsaved local state that
        // the hand-off's navigation to the main chat would discard.
        handOff={false}
        transcript={{
          sessionId: `embed:${slotKey}`,
          followOutput: !!startAtBottom,
          initialPlacement: startAtBottom ? 'bottom' : 'top',
          onAtBottomChange: setIsAtBottom,
          scrollerStyle: { paddingTop: 16, paddingBottom: 16, minHeight: 0 },
          // No hand-off: the embed's composer draft is unsaved local state that
          // the hand-off's navigation to the main chat would discard.
          earlier: { hasMore: canWiden, loading: widening, failed: widenFailed, onLoad: widenFailed ? retryWiden : widen, handOff: false },
          aboveRows: loadFailed ? (
            <div className="mx-4 my-3 flex items-start gap-2">
              <ErrorNotice className="flex-1" testId="chat-embed-load-error" message={i18nT('components.chatPane.history_load_failed')} />
              <button
                type="button"
                className="text-[12px] text-accent underline bg-transparent border-none cursor-pointer hover:text-accent-hover"
                onClick={() => { void refetch() }}
              >
                {i18nT('components.chatPane.retry')}
              </button>
            </div>
          ) : messages.length === 0 && !running && !titlePending ? (
            <div className="text-center text-muted text-[13px] py-10">{i18nT('appSdk.chatEmbed.session_ready_type_a_message_to_start')}</div>
          ) : undefined,
          // The main chat's working indicator (ChatFooter, shared with ChatPage
          // and ChatPane), after the last row, so a running turn reads as "the
          // reply is coming" where the reply will land. The poll carries the
          // stop phase but no compaction state, so the compaction branch never applies.
          belowRows: (
            <ChatFooter
              running={running}
              stopping={shown?.stopping ?? false}
              stopState={stopState}
              state={running ? 'streaming' : ''}
              lastRole={tail?.role ?? ''}
              streamTick={tail?.role === 'streaming' ? (tail.content?.length ?? 0) : 0}
              streamIdleMs={EMBED_STREAM_IDLE_MS}
            />
          ),
        }}
      />
      {/* The send-tail row is announced visually by the new-message scroll;
          a screen-reader user would otherwise only notice the button
          re-enabling. A polite live region carries the same text. */}
      {/* Announce the NEWEST outcome only: re-reading every live tail would
          replay the first failure each time a second one lands. */}
      <div role="status" aria-live="polite" className="sr-only">{sendTails.length ? sendTails[sendTails.length - 1].content : ''}</div>

      {startAtBottom && (
        <div className="relative">
          <JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={scrollToBottom} />
        </div>
      )}

      {/* No hand-off: the embed's composer draft is unsaved local state that
          the hand-off's navigation to the main chat would discard. */}
      {stalled && (
        <div className="shrink-0 mx-3 my-2 flex items-start gap-2">
          <ErrorNotice key={reconnectFailures} className="flex-1" testId="chat-embed-stalled" message={i18nT('appSdk.chatEmbed.connection_lost')} />
          <button
            type="button"
            className="text-[12px] text-accent underline bg-transparent border-none cursor-pointer hover:text-accent-hover disabled:cursor-default disabled:opacity-60"
            disabled={isFetching}
            aria-busy={isFetching}
            onClick={() => { void reconnect() }}
          >
            {i18nT('appSdk.chatEmbed.reconnect')}
          </button>
        </div>
      )}

      {aboveComposer && <div className="shrink-0">{aboveComposer}</div>}

      {/* fuseBelow off: the embed's input row is a flat strip, not the
          dock's glass the front card slides under, so the stack stands as
          whole cards above it. */}
      {(queuedMessages.length > 0 || systemDeliveryCount > 0) && (
        <div className="shrink-0 pt-1 pb-1" data-testid="chat-embed-queue">
          <SubagentDeliveryProgress count={systemDeliveryCount} />
          <QueueStack messages={queuedMessages} fuseBelow={false} />
        </div>
      )}

      {followUpOptions.length > 0 && (
        <div className={`shrink-0 px-3 ${frameless ? '' : 'bg-bg-accent'}`}>
          <FollowUpBar
            options={followUpOptions}
            picked={picked}
            onSelect={toggleOption}
            onSend={text => send(text)}
          />
        </div>
      )}

      {attachments.notice && (
        <div className={`shrink-0 px-3 pt-2 ${frameless ? '' : 'border-t border-border bg-bg-accent'}`}>
          {/* No hand-off: the unsaved composer draft and the staged attachments
              are local state that the hand-off's navigation would discard. */}
          <ErrorNotice
            testId="chat-embed-attach-notice"
            message={attachments.notice}
            onDismiss={attachments.dismissNotice}
            dismissLabel={i18nT('app.dismiss')}
          />
        </div>
      )}
      {attachments.files.length > 0 && (
        <div className="shrink-0">
          <FilePreviewStrip files={attachments.files} resizedInfo={attachments.resizedInfo} onRemove={attachments.removeFile} />
        </div>
      )}
      {/* What is on its way, from the moment it is picked: a chip only exists
          once the server returns a path, so without this a slow upload looked
          like nothing happened. */}
      {attachments.uploadingNames.length > 0 && (
        <div
          role="status"
          data-testid="chat-embed-uploading"
          className="shrink-0 flex items-center gap-2 px-3 pt-2 text-[12px] text-muted min-w-0"
        >
          <Loader2 size={14} className="animate-spin shrink-0" aria-hidden="true" />
          <span className="shrink-0">{i18nT('appSdk.chatEmbed.uploading_files')}</span>
          <span className="truncate" title={fmtList(attachments.uploadingNames)}>{fmtList(attachments.uploadingNames)}</span>
        </div>
      )}

      {/* The press hints describe the Stop button, so they show only while it
          is on screen; typing a draft swaps it for Send, which queues. The
          strip carries the line the composer row would draw, so it reads as
          the top of the composer, not as the transcript's last row; under an
          attachment strip the line is already drawn above. */}
      {(stopError || pressHint) && (
        <div className={`shrink-0 px-3 pt-2 ${frameless ? '' : `${attachments.notice || attachments.files.length || attachments.uploadingNames.length ? '' : 'border-t border-border '}bg-bg-accent`}`}>
          {stopError && (
            // A failed press that leaves a next press with a cost (the reset
            // hatch, or a force stop that drops queued messages) is said
            // without its own "press Stop again": the hint below names what
            // the next press does, so one instruction remains (GPT round 6).
            // No hand-off: the embed's composer draft is unsaved local state that the hand-off's navigation to the main chat would discard.
            <ErrorNotice
              testId="chat-embed-stop-error"
              message={pressHint ? i18nT('appSdk.chatEmbed.stop_did_not_go_through') : stopError}
            />
          )}
          {pressHint && <div className={stopError ? 'pt-1' : ''}>{pressHint}</div>}
        </div>
      )}

      <Composer slotKey={slotKey} value={draft} onChange={setDraft} capabilities={EMBED_CAPABILITIES}>
        <div
          className={`flex items-end gap-2 px-3 py-2 shrink-0 ${frameless ? '' : `${attachments.notice || attachments.files.length || attachments.uploadingNames.length || stopError || pressHint ? '' : 'border-t'} border-border bg-bg-accent`}`}
        >
          {attachments.granted && (
            <>
              <ComposerAttach
                ref={fileInputRef}
                multiple
                hidden
                tabIndex={-1}
                aria-hidden="true"
                data-testid="chat-embed-file-input"
                onFiles={attachments.addFiles}
              />
              <button
                type="button"
                className="p-2 rounded-md text-muted hover:text-text hover:bg-bg-elevated disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                onClick={() => fileInputRef.current?.click()}
                // Refused while an upload runs, as the main chat's picker is:
                // the spinner says files are on their way, not that it takes more.
                disabled={sendMutation.isPending || attachments.uploading}
                title={pickerLabel}
                aria-label={pickerLabel}
              >
                {attachments.uploading
                  ? <Loader2 size={16} className="animate-spin" aria-hidden="true" />
                  : <Paperclip size={16} aria-hidden="true" />}
              </button>
            </>
          )}
          <ComposerEditor
            ref={textareaRef}
            rows={1}
            {...composition}
            aria-label={i18nT('appSdk.chatEmbed.chat_message')}
            className="flex-1 min-w-0 min-h-[38px] resize-none overflow-y-auto px-3 py-2 mc-message-font-text bg-bg-elevated border border-border rounded-md text-text outline-hidden focus-visible:border-accent transition-colors"
            onKeyDown={e => submitOnEnter(e, () => send())}
            onPaste={attachments.onPaste}
            onDragOver={attachments.onDragOver}
            onDrop={attachments.onDrop}
            placeholder={running ? i18nT('appSdk.chatEmbed.agent_is_working') : (placeholder || i18nT('appSdk.chatEmbed.message'))}
            disabled={sendMutation.isPending}
          />
          {showStop ? (
            // The main composer's own Stop control; its hints are drawn above the
            // composer instead of beside the button, where a narrow embed has no
            // room for them.
            <StopControls stopState={stopState} killingEscaped={killingEscaped} stopWithTap={onStop} inlineHints={false} guideTarget={false} />
          ) : (
            <ComposerSend
              className="p-2 min-h-0 rounded-md disabled:opacity-40"
              onSend={() => send()}
              running={running}
              pending={sendMutation.isPending}
              hasAttachments={attachments.files.length > 0}
              uploading={attachments.uploading}
              sendLabel={i18nT('appSdk.chatEmbed.send_message')}
              sendTitle={i18nT('appSdk.chatEmbed.send')}
            />
          )}
        </div>
      </Composer>
    </div>
  )
}

export default ChatEmbed
