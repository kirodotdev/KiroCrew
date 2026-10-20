/**
 * The artifact's companion chat: the bound session resolved from the slots
 * snapshot, the short-lived context entry that names the artifact, creating or
 * replacing the bound session, the batch submit of pending comments, and
 * `sendNav`, the one dispatcher every leave-the-page affordance goes through.
 *
 * The `ArtifactDetailPage` facade calls each hook here at the point its code
 * sits in the page's hook order, which `ArtifactDetailPage.owners.test.tsx`
 * pins: React runs a component's effects in registration order, so moving one
 * is a behaviour change even when every test of the moved code still passes.
 */
import {
  useCallback, useMemo, useRef, useState, type Dispatch, type MutableRefObject,
  type SetStateAction,
} from 'react'
import { useMutation } from '@tanstack/react-query'
import { useAppSelector, useAppStore } from '../store'
import { switchSlot } from '../store/chatSlice'
import { fetchSlots, addSlotOptimistic, removeSlotOptimistic, armConfirmedCloseHold } from '../store/dashboardSlice'
import { api } from '../api/client'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { slotBusySteer } from '../components/chat-input/busySend'
import { useJevAutoSend } from './chat/useJevAutoSend'
import { useSentCommentIds } from '../hooks/useSentCommentIds'
import { formatArtifactCommentsMessage } from '../components/CommentOverlay'
import { pendingSubmitComments } from '../lib/commentFilter'
import { forwardToMain, type NavIntent } from '../utils/artifactPopout'
import { writePrefill } from '../utils/navIntent'
import type { ArtifactComment, ChatSlot } from '../types'

import { i18nT } from '../i18n/t'
import { byRecentActivity } from '../utils/slotRecency'
import type {
  ArtifactDetailPageStateRead, ArtifactDetailPageDocumentRead,
} from './useArtifactDetailPageDocument'

// Seconds. Without a TTL a dormant slot marches to the per-source ceiling and 429s every
// later post, so the TTL is what bounds this entry.
const COMPANION_CONTEXT_MAX_AGE_S = 3600

/** Human text for a rejected query/mutation, so every ErrorNotice on this page reads the same shape. */
/**
 * The artifact's active companion session: the bound slot for `slug`, or the most
 * recently active one if a race or a History-page resume left more than one.
 * Module-level so `openCompanionChat` can apply the identical rule to a freshly
 * fetched slots payload, not just the Redux snapshot.
 */
export function pickBoundSlot(slots: ChatSlot[] | undefined, slug: string): ChatSlot | null {
  const matches = (slots ?? []).filter((x) => x.artifact === slug)
  if (matches.length <= 1) return matches[0] ?? null
  // "Most recently active" has to be decided on INSTANTS. `last_activity_ts` is
  // the raw transcript `ts` and is not guaranteed to be one format, so comparing
  // the text can hand back the older session — which is the one case this
  // resolver exists to avoid, and the tie-break "New chat" depends on below.
  return [...matches].sort(byRecentActivity)[0]
}

/** Carries the SLUG as well as the version: the retraction is artifact-scoped, and this page
 *  survives a slug change, so a bare version cannot say which artifact a result belongs to. */
type InjectVars = { slotKey: string; version: number; slug: string; refresh?: boolean }

/** The companion chat session, its context entry and the batch submit of pending comments. */
export function useArtifactDetailPageCompanion({
  stateRead, documentRead, durableComments, setPanel, sidebarUserToggledRef,
  buildCompanionContext, popout,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
  durableComments: ArtifactComment[]
  setPanel: Dispatch<SetStateAction<'none' | 'comments' | 'chat'>>
  sidebarUserToggledRef: MutableRefObject<boolean>
  buildCompanionContext: () => string
  popout: boolean
}) {
  const {
    slug, navigate, dispatch, setSaveError, setContextError, setContextErrorIsRefresh, contextFailureMessage,
  } = stateRead
  const { artifact } = documentRead
  /**
   * Single navigation dispatcher for every affordance that leaves the artifact
   * view. In the main dashboard it navigates locally (seeding the composer
   * prefill / active slot first). Inside a popout window it must NOT touch the
   * router — an in-window navigate() would remount the entire dashboard inside
   * the popout — so the intent is forwarded to a main dashboard window (or a
   * new tab when none is alive) and this window stays pinned to its artifact.
   */
  const sendNav = useCallback((intent: NavIntent) => {
    if (popout) { forwardToMain(intent); return }
    if (intent.prefill) writePrefill(intent.prefill.slotKey, intent.prefill.prompt)
    if (intent.slotKey) dispatch(switchSlot(intent.slotKey))
    navigate(intent.path)
  }, [popout, dispatch, navigate])

  // ── Companion chat ─────────────────────────────────────────────────────────
  // The active bound session is resolved from the Redux slots snapshot (the WS
  // `slots` event carries each slot's `artifact` binding) — zero extra
  // endpoints. The frontend flow keeps it to <=1 active bound session per slug
  // (archive-then-create); the resolver tolerates more by picking the most
  // recently active, so races and history resumes degrade gracefully.
  // `?? []` mirrors TaskProgressBar: the slots array is typed non-optional but
  // the store hydrates from localStorage, so a stale persisted shape can hand
  // back undefined and a bare .filter() would crash the whole artifact page.
  const slots = useAppSelector((s) => s.dashboard.slots)
  // `slots` is `[]` BOTH before the first fetch and when nothing is bound, so the
  // empty array alone cannot be read as "no bound session" — see openCompanionChat.
  const slotsLoaded = useAppSelector((s) => s.dashboard.slotsLoaded)
  // The winner drives the panel; the full set matters when archiving, because a
  // two-window creation race can leave more than one slot bound to this slug and
  // archiving only the winner lets pickBoundSlot reopen the leftover.
  const boundSlots = useMemo(
    () => (slots ?? []).filter((x) => x.artifact === slug),
    [slots, slug],
  )
  const boundSlot = useMemo(() => pickBoundSlot(slots, slug), [slots, slug])
  const [chatCreating, setChatCreating] = useState(false)
  // Gateway connection flag: the chat send path refuses silently while it is
  // false, so the batch submit is disabled (and bails) there — same gating as
  // the file viewer's "Submit All".
  const connected = useAppSelector((s) => s.dashboard.connected)
  // The batch submit takes the bound session's busy decision at press time.
  const appStore = useAppStore()
  const jevAutoConsented = useJevAutoSend()
  // Serializes the two session-lifecycle entry points. `chatCreating` cannot do
  // this job: it is React state (so a second handler in the same tick still sees
  // the old value) and it is only set INSIDE createBoundSession, which runs
  // AFTER newCompanionChat's `await` on the archive — leaving a window where a
  // rapid double-click starts two flows, both resolve the same boundSlot, both
  // archive it (the second 404s and proceeds by design), and both create a
  // replacement. That yields two active bound sessions for one artifact, the
  // exact invariant the archive-then-create ordering exists to protect.
  const sessionOpBusyRef = useRef(false)
  // MEMORY ONLY, and scoped to this page's lifetime. A resolved POST is not a durable-write
  // acknowledgment, so a claim that outlived the page could skip a nudge for a lost entry.
  const injectedVersionRef = useRef<Map<string, number>>(new Map())
  /** Drop the claim after a rejected POST, so the next open retries. Guarded on the
   *  version: a stale rejection must not delete a newer request's claim. */
  const releaseInjectedVersion = useCallback((slotKey: string, version: number) => {
    if (injectedVersionRef.current.get(slotKey) === version) {
      injectedVersionRef.current.delete(slotKey)
    }
  }, [])

  /** Create a fresh bound session with a ONE-round-trip critical path: the
   *  create response — which carries the `artifact` binding — is dispatched
   *  straight into the Redux slots list (addSlotOptimistic), so `boundSlot`
   *  resolves and the panel becomes interactive immediately. The silent context
   *  entry and the server-truth refresh run in the background: the context POST
   *  completes long before a human can type and send (it is consumed on the NEXT
   *  user message), and fetchSlots/WS snapshots reconcile the optimistic row
   *  with full slot metadata moments later.
   *
   *  `prefillText` is staged via writePrefill BEFORE the optimistic bind so
   *  ChatPage's slot-activation effect deterministically finds it on mount. */
  /**
   * The silent /context write, routed through `useMutation` like every other write on this
   * page (use-react-query guideline) rather than a bare promise chain.
   *
   * The hold is taken by the CALLER, before `mutate`, so an open/close/reopen inside the
   * request window cannot inject twice; only a resolved write persists the claim.
   */
  // The NEWEST injection requested, keyed by artifact AND slot. A ref, not state: it gates a
  // retraction inside a callback and must never itself cause a render.
  const latestInjectedRequest = useRef<{ slug: string; slotKey: string; version: number }>(
    { slug: '', slotKey: '', version: 0 },
  )

  const injectContextMut = useMutation({
    mutationFn: (vars: InjectVars) =>
      api.chatSlotContext(vars.slotKey, buildCompanionContext(), {
        source: 'artifact-companion', ephemeral: true, maxAge: COMPANION_CONTEXT_MAX_AGE_S,
      }),
    onSuccess: (_d: unknown, vars: InjectVars) => {
      injectedVersionRef.current.set(vars.slotKey, vars.version)
      // ARTIFACT-SCOPED: a version number is not an identity, and this page outlives a slug
      // change, so another artifact's success must not retract THIS artifact's failure.
      if (vars.slug !== slug) return
      const high = latestInjectedRequest.current
      // Newest request on the CURRENT slot only: an older success, or a superseded session's
      // late success at the same version, would clear a notice this write cannot satisfy.
      if (high.slug === slug && high.slotKey === vars.slotKey && vars.version >= high.version) {
        setContextError(null)
      }
    },
    onMutate: (vars: InjectVars) => {
      const high = latestInjectedRequest.current
      // The high-water mark RESTARTS per artifact: carrying a taller version across a slug
      // change would suppress the next legitimate retraction on the artifact now shown.
      // A new slot restarts it too: the notice now belongs to that session.
      latestInjectedRequest.current = high.slug === vars.slug && high.slotKey === vars.slotKey
        ? { slug: vars.slug, slotKey: vars.slotKey, version: Math.max(high.version, vars.version) }
        : { slug: vars.slug, slotKey: vars.slotKey, version: vars.version }
    },
    onError: (_err: unknown, vars: InjectVars) => {
      // Not a delivery, so the claim is released and the next open RETRIES rather than
      // recording a baseline -- see the resume path.
      releaseInjectedVersion(vars.slotKey, vars.version)
      if (vars.slug !== slug) return
      setContextErrorIsRefresh(vars.refresh === true)
      setContextError(contextFailureMessage(vars.refresh === true))
    },
  })

  // NO AUTO-RETRACTION ON A TURN, matching the co-author surface: a turn does not attach the
  // artifact, so clearing on one tells a user who typed anything at all that the agent has it.

  // NO RETRY either. Dismissal or a later successful share is what clears the notice.

  const createBoundSession = useCallback(async (prefillText?: string): Promise<string | null> => {    if (!artifact) return null
    setChatCreating(true)
    try {
      // No `name`: the backend generates a unique slot key (reusing a
      // name-derived key would append onto an archived session's history file).
      // The pinned title keeps the sidebar readable.
      const res = await api.createChatSlot(
        undefined, undefined, undefined, undefined, undefined,
        i18nT('pages.artifactDetailPage.session_title', { name: artifact.name }), artifact.slug,
        undefined, undefined, true,
      )
      if (prefillText) writePrefill(res.key, prefillText)
      dispatch(addSlotOptimistic({
        key: res.key,
        title: res.title || i18nT('pages.artifactDetailPage.session_title', { name: artifact.name }),
        messages: 0,
        running: false,
        artifact: artifact.slug,
      } as ChatSlot))
      // Activate the bound slot NOW — back-to-back with writePrefill, mirroring
      // ChatPage's follow-up worktree handler — so activeSlot is already this
      // slot BEFORE boundSlot resolves and ArtifactChatPanel mounts the embedded
      // ChatPage. Otherwise the switch lands in the same commit as that mount,
      // where ChatPage's mount-only slot re-fetch effect (StrictMode
      // double-invoked, empty deps → stale activeSlot capture) re-asserts the
      // PRIOR active slot as the last write. activeSlot then never becomes this
      // slot on first open, so the per-slot draft-restore effect can't consume
      // the staged prefill and the composer opens empty (correct only on the
      // second open). Idempotent once active === res.key.
      dispatch(switchSlot(res.key))
      // Held BEFORE the POST so a concurrent reopen cannot inject twice; the hold is
      // memory-only and only a RESOLVED post persists the claim.
      const injectedVersion = artifact.version
      injectedVersionRef.current.set(res.key, injectedVersion)
      injectContextMut.mutate({ slotKey: res.key, version: injectedVersion, slug })
      dispatch(fetchSlots())
      return res.key as string
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
      return null
    } finally {
      setChatCreating(false)
    }
  }, [artifact, dispatch, injectContextMut, slug, setSaveError])

  /** "New chat": archive the current bound session FIRST (the existing red-X
   *  delete path — history preserved, resumable from the History page), then
   *  create fresh — so the <=1-active invariant never observably breaks. The
   *  optimistic remove mirrors the optimistic add: without it the resolver would
   *  keep picking the archived slot (it has a last_activity_ts, the new one
   *  doesn't) until the next WS snapshot prunes it. */
  const newCompanionChat = useCallback(async () => {
    // Reject a re-entrant click rather than queue it: the second click's intent
    // ("give me a fresh session") is already satisfied by the first.
    if (sessionOpBusyRef.current) return
    sessionOpBusyRef.current = true
    try {
      // Archive every slot bound to this slug, not just the resolved winner.
      for (const slot of boundSlots) {
        try {
          await api.deleteChatSlot(slot.key)
        } catch (err) {
          // ONLY a 404 means "already archived, nothing to do". Any other
          // failure means the old session is still live server-side, so creating
          // anyway would leave TWO bound sessions for this slug — and since the
          // resolver breaks ties on last_activity_ts, the OLD one keeps winning
          // and "New chat" silently appears to do nothing. Abort and say why.
          //
          // Read `status` structurally rather than via `instanceof ApiError`:
          // this fails closed on anything not provably a 404 (including an error
          // re-thrown or wrapped across a module boundary, where instanceof
          // silently stops matching), and it is verifiable in a test.
          const status = (err as { status?: unknown } | null | undefined)?.status
          if (status !== 404) {
            setSaveError(err instanceof Error ? err.message : String(err))
            return
          }
        }
        dispatch(armConfirmedCloseHold(slot.key))
        dispatch(removeSlotOptimistic(slot.key))
      }
      await createBoundSession()
    } finally {
      sessionOpBusyRef.current = false
    }
  }, [boundSlots, createBoundSession, dispatch, setSaveError])

  // ── batch submit to the companion session ──
  // Sent-id tracking is shared with the chat side panel's Submit.
  const { sentIds, markSent } = useSentCommentIds(slug)
  // Pending = forwarding-eligible AND human-authored AND not yet submitted —
  // `pendingSubmitComments`, the same composition as ArtifactPanel, because
  // this is the standalone-page twin of that Submit and both reach the same
  // agent. Agent comments are dropped there AND inside
  // formatArtifactCommentsMessage (hardened esc()).
  const pendingComments = useMemo(
    () => pendingSubmitComments(durableComments, sentIds),
    [durableComments, sentIds],
  )
  const pendingCommentIds = useMemo(() => new Set(pendingComments.map(c => c.id)), [pendingComments])
  const [submittingComments, setSubmittingComments] = useState(false)
  /** Send every pending comment to the artifact's companion session as ONE
   *  message — the standalone-page twin of the chat side panel's Submit.
   *
   *  Offered ONLY while a session is bound (see the render in ArtifactDetailPageBody.tsx), so the bar
   *  never promises a send it cannot make: with none bound it would have to
   *  decide whether this artifact has one, and a wrong answer there opens a
   *  second companion chat. Unbound, the footer's "Draft a prompt in chat" is the
   *  affordance, and it says what it does. The `boundSlot` test here is the
   *  backstop for that, not a second flow. */
  const submitCommentsToChat = useCallback(async (extraPrompt?: string) => {
    // Nothing sent: `false`, so the bar hands the typed instruction back.
    if (!connected || !artifact || !boundSlot || pendingComments.length === 0) return false
    const batch = pendingComments
    setSubmittingComments(true)
    try {
      // A Send press on the bound session: it steers or queues per that
      // session's busy-send mode. A steer comes back `dispatched` (steered, or
      // a fresh turn) or `queued` (demoted), so the custody rule below holds.
      const steer = slotBusySteer(appStore.getState(), boundSlot.key, jevAutoConsented)
      const receipt = await sendTurn({
        message: formatArtifactCommentsMessage(slug, artifact.name, batch, extraPrompt),
        slot: boundSlot.key,
        ...(steer ? { steer } : {}),
      })
      // Mark sent only on a receipt that PROVES the server took custody: a
      // dispatch, a queue entry, or a 2xx whose body would not parse (accepted,
      // only the answer was mangled). Everything else keeps the batch pending.
      //
      // That is stricter than the composer's rule on this transport, on purpose.
      // ChatPage can afford to read a refusal or a late answer optimistically
      // because its payload stays on screen — the optimistic row holds the text
      // and says it is unconfirmed. Here the payload is a set of ids in
      // `mc-cmt-sent:<slug>`, the set is append-only, and no UI clears it: a
      // batch marked sent for a POST that never arrived is a review nobody can
      // re-offer. So an abort deadline (`response-late`) and a rejected fetch
      // (`transport-error`) both leave it pending, and the cost of being wrong
      // is one duplicate turn the user chooses, not a submitted review that is
      // silently gone.
      //
      // A refusal goes back to the bar, which frames the server's reason
      // beside its button (sendTurn: the reason is fit to show).
      if (receipt.status !== 'dispatched' && receipt.status !== 'queued' && receipt.status !== 'unknown') {
        return receipt.reason || false
      }
      markSent(batch)
      // Show the session the batch landed in; the panels are mutually exclusive,
      // so this is what replaces the chat's own sent-message echo.
      sidebarUserToggledRef.current = true
      setPanel('chat')
      return true
    } finally {
      setSubmittingComments(false)
    }
  }, [connected, artifact, pendingComments, boundSlot, slug, markSent, appStore, jevAutoConsented, setPanel, sidebarUserToggledRef])

  /** Full-page escape hatch — routes through sendNav so a popout forwards the
   *  intent to a main window instead of remounting the dashboard in-frame. */
  const openChatFull = useCallback(() => {
    if (!boundSlot) return
    sendNav({ path: '/chat', slotKey: boundSlot.key })
  }, [boundSlot, sendNav])

  return {
    sendNav, slotsLoaded, boundSlot, chatCreating, connected, sessionOpBusyRef, injectedVersionRef,
    injectContextMut, createBoundSession, newCompanionChat, pendingComments, pendingCommentIds,
    submittingComments, submitCommentsToChat, openChatFull,
  }
}

export type ArtifactDetailPageCompanionRead = ReturnType<typeof useArtifactDetailPageCompanion>
