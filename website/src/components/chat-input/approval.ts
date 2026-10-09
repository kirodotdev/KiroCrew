import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { shallowEqual } from 'react-redux'
import { useAppSelector, type useAppDispatch } from '../../store'
import { resolveApprovalRow, resolveShownApprovalRow, openActivityToTool, openActivityToTab, selectSlotMessages, selectSlotPendingApproval, selectSlotPendingSpawnApprovals, markSubagentApproving, sseSubagentDone, endGoneSpawnApproval } from '../../store/chatSlice'
import { useToolPillVisible } from '../../store/toolPillRegistry'
import { api } from '../../api/client'
import { isNotFoundError, isTerminalApprovalRefusal, noPendingApprovalError } from '../../api/apiError'
import { safeSetItem, safeGetItem } from '../../utils/safeStorage'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { pickToolLabel } from '../../utils/toolLabel'
import { deriveToolCallTitle } from '../../utils/toolCallTitle'
import { toApiDecision } from '../../utils/approvalDecision'
import { permissionRowTarget, type CoordinatorApprovalTarget } from '../../types/approvalTarget'
import { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../ApprovalModePicker'
import type { SubagentActivity } from '../../types'
import { i18nT } from '../../i18n/t'

/* The slot's approval chrome as state: the pending tool approval (its
   decision submit, the approval-mode discovery hint and nudge, the ghost
   pill mirror, the failure notice) and the pending sub-agent spawns. The
   composer renders the bar; `SpawnApprovalCard` renders the spawns. */

type AppDispatch = ReturnType<typeof useAppDispatch>
// One-shot decisions sent through `api.decideApproval` are
// mapped by the shared `toApiDecision` (utils/approvalDecision.ts), which is
// fail-closed and is the only place that mapping is spelled — see that module
// for why a local ternary here cannot be caught by any downstream guard (#5400,
// #5434, #5486). The Trust affordances are withheld from this path at their
// render sites (`approvalTrustGrantable`); a trust verb that reaches the mapping
// anyway is rejected rather than silently upgraded.

/** Approval sources that run unattended, with no human bound to the chat the
 *  card renders in. Session-scoped Trust is meaningless for these (see
 *  `approvalIsUnattended`), so the Trust controls are withheld and only
 *  Allow once / Reject are offered. Kept in sync with the backend's
 *  `_BACKGROUND_APPROVAL_SOURCES` minus `autonudge`, which does run in-session. */
export const UNATTENDED_APPROVAL_SOURCES = new Set(['cron', 'heartbeat', 'taskrunner'])

/** B2 nudge: after this many manual one-shot approvals in one slot while the
 *  mode is still `normal`, offer the approval-mode picker once. Three is the
 *  point where repeated prompting reads as friction rather than safety. */
const APPROVAL_NUDGE_THRESHOLD = 3

// Pending-approval selection is slot-aware — see selectSlotPendingApproval
// in chatSlice: each grid pane's approval bar reflects ITS slot.

/** Stable empty result for suppressed spawn-approval reads — a fresh [] per render would churn every dependent memo. */
const EMPTY_SPAWN_APPROVALS: ReturnType<typeof selectSlotPendingSpawnApprovals> = []

export const approvalBtnClass = 'inline-flex items-center gap-1 px-2 py-1 rounded-md bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border border-border text-text text-[12px] cursor-pointer font-body hover:bg-[color-mix(in_srgb,var(--warn)_25%,transparent)] hover:text-text hover:border-border-strong transition-colors disabled:opacity-50'

export function useToolApproval({ slotId, slotApprovalChrome, approvalMode, dispatch }: {
  slotId: string | null
  slotApprovalChrome: boolean
  approvalMode?: string
  dispatch: AppDispatch
}) {
  const pendingApprovalRaw = useAppSelector(s => selectSlotPendingApproval(s, slotId), shallowEqual)
  // Suppressed at the READ so every consumer (bar, ghost, pill, rounded-corner
  // class) follows one judgment instead of each render site re-deciding.
  const pendingApproval = slotApprovalChrome ? pendingApprovalRaw : null
  // Where the bar's row sits, so a refusal of a row that names no request
  // settles that row and no other (resolveShownApprovalRow).
  const pendingApprovalIndex = useAppSelector(s => {
    const shown = selectSlotPendingApproval(s, slotId)
    return shown ? (slotId ? selectSlotMessages(s, slotId) : s.chat.messages).indexOf(shown) : -1
  })
  const hasApproval = !!pendingApproval
  const [approvalSubmitting, setApprovalSubmitting] = useState(false)
  // A2: bumping this opens the footer ApprovalModePicker with a spotlight
  // ring, so the approval bar's hint lands the user on the real control.
  const [approvalPickerSignal, setApprovalPickerSignal] = useState(0)
  // A1: the hint retires once the user has ever adjusted the mode themselves.
  // Read per approval arrival (cheap), not once per mount, so adjusting the
  // mode hides the hint on the very next approval without a reload.
  const approvalModeAdjusted = !!pendingApproval && !!safeGetItem(APPROVAL_MODE_ADJUSTED_LS_KEY)
  // B2: per-slot manual one-shot approval tally for this dashboard session.
  // In-memory by design — "3 approvals in one sitting" is the annoyance
  // signal; persisting it would fire the nudge on stale history.
  const approvalCountsRef = useRef<Record<string, number>>({})
  const [approvalNudgeSlot, setApprovalNudgeSlot] = useState<string | null>(null)
  const approvalNudgeActive = !!approvalNudgeSlot && approvalNudgeSlot === slotId
  // Permanent dismissal (buttons / menu open): the callout has delivered its
  // lesson, so the A1 hint retires with it — otherwise a "Got it" user keeps
  // seeing "Tired of confirming every step?" on every later approval.
  const dismissApprovalNudge = useCallback(() => {
    setApprovalNudgeSlot(null)
    // One flag carries both retirements: the adjusted/discovery flag already
    // suppresses the hint AND gates the nudge, so a separate dismissed flag
    // would only ever be written alongside it — dead state.
    safeSetItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
  }, [])
  // Session-scoped hide (Escape): a reflexive Escape aimed at the composer
  // must not spend the one-time callout unseen; it may re-fire on a later
  // approval in this sitting.
  const hideApprovalNudge = useCallback(() => setApprovalNudgeSlot(null), [])
  // Non-null while the last approval decision failed. Rendered as a one-line
  // strip under the composer; auto-clears so it cannot become permanent chrome.
  const [approvalNotice, setApprovalNotice] = useState<string | null>(null)
  // The same notice slot carries two different things: STATUS about an
  // approval that expired (nothing failed on our side) and a FAILED decision
  // submit (a rejected request). Only the latter is an error surface.
  const [approvalNoticeKind, setApprovalNoticeKind] = useState<'status' | 'error'>('status')

  const activeSlot = slotId
  const approvalMeta = pendingApproval?.meta as Record<string, unknown> | undefined
  const approvalId = approvalMeta?.approval_id as string | undefined
  /** The request this card shows (types/approvalTarget). Every decide is
   *  bound to it: the id recurs, and a chat runner's id can collide with a
   *  coordinator one. */
  const approvalTarget = useMemo(() => permissionRowTarget(approvalMeta, slotId), [approvalMeta, slotId])
  const approvalToolInput = (approvalMeta?.tool_input as string) || ''
  const approvalIsReadOnly = !!(approvalMeta?.is_read_only)
  const approvalFullCommand = (approvalMeta?.full_command as string) || ''
  const approvalBaseCommand = (approvalMeta?.base_command as string) || ''
  const approvalIsShell = approvalMeta?.is_shell === '1'
  // Command-scoped trust is offered only when the gateway proved a canonical,
  // unredacted scope.  The title/input preview are presentation data and must
  // never be promoted into grant authority by a frontend fallback.
  const approvalTrustCommandGrantable = approvalMeta?.trust_command_grantable === '1'
  const approvalTrustBaseGrantable = approvalMeta?.trust_base_grantable === '1'
  /** Server proof that the SESSION-wide grant ("trust all tools") can be
   *  recorded for this card. Read separately from the command-scoped bit above
   *  because the session grant names no command: it auto-approves whatever this
   *  slot asks for next. Reusing the command bit for it hid the whole menu
   *  whenever the transport redacted or could not canonicalize the command, so
   *  a card that could still take a session grant offered allow-once and reject
   *  alone. */
  const approvalTrustAllGrantable = approvalMeta?.trust_grantable === '1'
  /** Sources that run with no human attached to THIS conversation. Session
   *  trust means "auto-approve tools for this chat session", which is
   *  incoherent for an unattended job: the job is not this session, so the
   *  grant would widen this slot's own auto-approval surface while doing
   *  nothing for the job. `autonudge` is deliberately absent — a monitor loop
   *  runs *in* this session, so trusting it is meaningful. */
  const approvalSource = (approvalMeta?.source as string)
    // Persisted permission rows are rehydrated from content alone (chatSlice's
    // reconstruct path carries no `source`), so fall back to the `[source]`
    // prefix the card was written with rather than silently treating a
    // reloaded cron card as an ordinary in-session one.
    || (pendingApproval?.content || '').match(/^(?:🔧\s*)?\[([a-z_]+)\]/)?.[1]
    || ''
  const approvalIsUnattended = UNATTENDED_APPROVAL_SOURCES.has(approvalSource)
  /** True when a standing Trust grant can actually be RECORDED for this card.
   *  FAIL-CLOSED: the Trust affordances are withheld unless this holds, because
   *  the only other resolve path is a one-shot `api.decideApproval`, which
   *  has no trust verb — offering Trust there claims a standing grant the
   *  backend never records (#5400, #5434, #5486).
   *  - `activeSlot`: `api.approveChatSlot` is slot-scoped, so with no slot the
   *    grant has nowhere to land and `handleApprovalAction` falls through to the
   *    one-shot endpoint.
   *  - `!approvalIsUnattended`: session trust is incoherent for a job that is
   *    not this session (see `approvalSource` above). */
  const approvalTrustGrantable = !!activeSlot && !approvalIsUnattended
    // A standing grant is recorded on the slot that raised the request, which
    // only a chat runner's own request has: a coordinator approval parked in
    // this chat is answered one-shot, by its own target.
    && approvalTarget?.origin === 'native'
  const simplified = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  const approvalLabelRaw = sanitizeLlmOutput(pendingApproval?.content || '').replace(/^🔧\s*/, '')

  const approvalToolCallId = (approvalMeta?.tool_call_id as string) || null

  const approvalToolEntry = useAppSelector(s => {
    if (!approvalToolCallId) return null
    const log = slotId && slotId !== s.chat.activeSlot ? (s.chat.slotActivity[slotId]?.toolLog ?? []) : s.chat.toolLog
    const entry = log.findLast(e => e.type === 'tool' && e.tool_call_id === approvalToolCallId)
    return entry ? { purpose: entry.purpose || '', ts: entry.ts || 0 } : null
  }, shallowEqual)
  const approvalPurpose = approvalToolEntry?.purpose || ''
  const approvalTs = approvalToolEntry?.ts || 0

  // The same label rule as the tool pill (ToolCallLine): simplified mode shows
  // the purpose, else the argument-derived title; raw mode keeps the verbatim
  // title unless it is a stub. The permission meta carries `tool_kind` /
  // `is_shell` / `tool_name` / `mcp_server` for exactly this derivation, and the
  // verbatim command stays in the ghost pill's ToolDetails payload — the human vets
  // the bytes, the title only says what they do.
  const approvalDerived = deriveToolCallTitle({
    title: approvalLabelRaw,
    kind: (approvalMeta?.tool_kind as string) || '',
    rawInput: approvalMeta?.tool_input,
    isShell: approvalMeta?.is_shell === '1' || approvalMeta?.is_shell === true,
    toolName: (approvalMeta?.tool_name as string) || '',
    mcpServer: (approvalMeta?.mcp_server as string) || '',
  })
  const approvalLabel = pickToolLabel({ simplified, purpose: approvalPurpose, rawLabel: approvalLabelRaw, derivedTitle: approvalDerived.title, uiLang })

  // Subscribe to the inline pill's viewport visibility. While the pill is in
  // view, the bar collapses to just the always-visible button row; the moment
  // the pill scrolls past the top, a "ghost pill" mirror slides into the bar
  // so the user keeps full context (timestamp, purpose, input preview)
  // alongside the action buttons. See src/store/toolPillRegistry.ts.
  const pillVisible = useToolPillVisible(approvalToolCallId)

  // Settle guard: when a new approval arrives, suppress the ghost for a brief
  // window so the Virtuoso list has time to mount the ToolCallLine and register
  // the pill. Without this, the ghost flashes for 1-2 frames then collapses
  // once the in-chat pill reports itself visible.
  const [ghostSettled, setGhostSettled] = useState(false)
  useEffect(() => {
    if (!approvalToolCallId) { setGhostSettled(false); return }
    setGhostSettled(false)
    const t = setTimeout(() => setGhostSettled(true), 150)
    return () => clearTimeout(t)
  }, [approvalToolCallId])

  const showGhost = !!pendingApproval && !pillVisible && ghostSettled

  // Auto-dismiss the failure notice. Bounded lifetime keeps a transient
  // backend hiccup from leaving a permanent banner over the composer.
  useEffect(() => {
    if (!approvalNotice) return
    const t = setTimeout(() => setApprovalNotice(null), 8000)
    return () => clearTimeout(t)
  }, [approvalNotice])
  const showInChat = useCallback(() => {
    if (approvalToolCallId) dispatch(openActivityToTool(approvalToolCallId))
  }, [approvalToolCallId, dispatch])

  const handleApprovalAction = useCallback((decision: string, pattern?: string) => {
    if (!approvalId) return
    setApprovalSubmitting(true)
    setApprovalNotice(null)
    const settle = (outcome: string) => {
      if (approvalTarget) dispatch(resolveApprovalRow({ target: approvalTarget, decision: outcome }))
      else if (pendingApprovalIndex >= 0) {
        const key = (approvalMeta?.clientTs as string | undefined) ?? pendingApproval?.ts
        dispatch(resolveShownApprovalRow({ slot: activeSlot, index: pendingApprovalIndex, key, id: approvalId, decision: outcome }))
      }
    }
    const finish = () => {
      settle(decision)
      setApprovalSubmitting(false)
      // B2: tally manual one-shot approvals per slot. Only 'approved' counts —
      // a trust grant already reduces future prompts, and a rejection is not
      // approval fatigue. Fires once per dashboard install (localStorage
      // guard) and only while the slot still asks about everything (normal).
      if (decision === 'approved' && activeSlot && !approvalIsUnattended) {
        const n = (approvalCountsRef.current[activeSlot] || 0) + 1
        approvalCountsRef.current[activeSlot] = n
        if (
          n >= APPROVAL_NUDGE_THRESHOLD &&
          approvalMode === 'normal' &&
          !safeGetItem(APPROVAL_MODE_ADJUSTED_LS_KEY)
        ) {
          setApprovalNudgeSlot(activeSlot)
        }
      }
    }
    const fail = (err: unknown) => {
      setApprovalSubmitting(false)
      // 404 means the backend no longer holds a future for this id — the turn
      // was stopped, timed out, or the process was replaced. The card is an
      // orphan: leaving it up makes every button look broken, so clear it and
      // say why instead of only logging to the console.
      if (isNotFoundError(err)) {
        settle('stale')
        // Say WHOSE request timed out when it was unattended: those sources
        // deny-fast on a short window, so by the time a human reads the card
        // the job has usually been denied and moved on. Otherwise the sentence
        // every approval surface uses for a refused press: the request may have
        // expired, been decided elsewhere or been replaced, and a 404 does not
        // say which.
        setApprovalNoticeKind('status')
        setApprovalNotice(
          approvalIsUnattended
            ? i18nT('components.chatInput.that_request_already_timed_out_and_was_denied', { source: approvalSource })
            : i18nT('components.approvalCard.approval_no_longer_pending')
        )
        return
      }
      // eslint-disable-next-line no-console -- surface real approval-resolution failures to the dev console
      console.error('Approval failed:', err)
      setApprovalNoticeKind('error')
      setApprovalNotice(i18nT('components.chatInput.could_not_submit_that_decision_see_the_console_f'))
    }
    // A card that names no request has nothing live behind it.
    if (!approvalTarget) {
      fail(noPendingApprovalError())
      return
    }
    if (['trust_command', 'trust_base', 'trust', 'trust_reads'].includes(decision) && activeSlot) {
      // Defence in depth: the Trust controls are not rendered for unattended
      // sources or coordinator requests, but never let a trust grant be applied
      // on their behalf. The grant would land on THIS slot (api.approveChatSlot
      // is slot-scoped), widening its auto-approval surface for work that is not
      // this session. Downgrade to a one-shot allow instead of over-granting.
      if (approvalIsUnattended || approvalTarget.origin !== 'native') {
        api.decideApproval(approvalTarget, 'approve').then(finish).catch(fail)
        return
      }
      // Bound to the row this card showed, like every other decide: a stale
      // card's grant must not land on a same-id request that replaced it.
      const extra: Record<string, string> = { request_id: approvalTarget.id, origin: 'native', request_mid: approvalTarget.mid }
      if (pattern) extra.pattern = pattern
      api.approveChatSlot(activeSlot, decision, extra).then(finish).catch(fail)
    } else {
      api.decideApproval(approvalTarget, toApiDecision(decision)).then(finish).catch(fail)
    }
  }, [approvalId, approvalTarget, approvalMeta, pendingApproval, pendingApprovalIndex, activeSlot, approvalIsUnattended, approvalSource, approvalMode, dispatch])

  return {
    pendingApproval, hasApproval, approvalId, approvalSubmitting, approvalPickerSignal, setApprovalPickerSignal,
    approvalModeAdjusted, approvalNudgeActive, dismissApprovalNudge, hideApprovalNudge,
    approvalNotice, setApprovalNotice, approvalNoticeKind,
    approvalToolInput, approvalIsReadOnly, approvalFullCommand, approvalBaseCommand, approvalIsShell,
    approvalTrustCommandGrantable, approvalTrustBaseGrantable, approvalTrustAllGrantable, approvalIsUnattended, approvalTrustGrantable,
    approvalLabelRaw, approvalToolCallId, approvalPurpose, approvalTs, approvalLabel, showGhost, showInChat, handleApprovalAction,
  }
}

export function useSpawnApprovals({ slotId, slotApprovalChrome, dispatch }: {
  slotId: string | null
  slotApprovalChrome: boolean
  dispatch: AppDispatch
}) {
  // Pending sub-agent SPAWN approvals for this slot (blocked on user approval).
  // Surfaced as a top-level banner with inline Approve/Reject so the user can
  // resolve pending spawns without leaving the composer. A single pending spawn
  // gets a compact one-line row; with several, the header carries Approve all /
  // Reject all and each sub-agent gets its own row with per-agent Approve/Reject
  // (so one can be run and another rejected). "Review in panel" opens the
  // Subagents tab for the fuller per-agent view (task + streaming output).
  // Resolution goes through the same api.decideApproval + markSubagentApproving
  // path the panel uses, so the two surfaces stay consistent for a given id.
  const pendingSpawnApprovalsRaw = useAppSelector(s => selectSlotPendingSpawnApprovals(s, slotId), shallowEqual)
  const pendingSpawnApprovals = slotApprovalChrome ? pendingSpawnApprovalsRaw : EMPTY_SPAWN_APPROVALS
  const reviewSpawnApprovals = useCallback(() => { dispatch(openActivityToTab('subagents')) }, [dispatch])
  // True once every pending spawn is mid-resolution — swaps the header buttons
  // for a "Resolving…" note. Cards stay in the pending list (status is still
  // 'pending') until the backend confirms, so the banner remains mounted.
  const spawnApprovalsResolving = pendingSpawnApprovals.length > 0 && pendingSpawnApprovals.every(a => a.approving)
  // A spawn with nothing live behind it (no target, or a terminal refusal)
  // ends here the way a rejected one does: its Subagents card records why,
  // and the banner, which lists only pending spawns, lets it go. A later
  // spawn or pending frame for the same id replaces this ending. A
  // retryable failure keeps the buttons, as on the base.
  // Only a card still pending on the request the press named ends: another
  // tab can win the decision, its frame marks the card running, and this
  // tab's late refusal then leaves the running spawn alone
  // (`endGoneSpawnApproval`). The ended Subagents card carries the refusal.
  const endGoneSpawn = useCallback((a: SubagentActivity, target: CoordinatorApprovalTarget | null) => {
    if (!slotId) return
    dispatch(endGoneSpawnApproval({ slot: slotId, id: a.id, target, error: i18nT('components.approvalCard.approval_no_longer_pending') }))
  }, [dispatch, slotId])
  const resolveOneSpawn = useCallback((a: SubagentActivity, action: 'approve' | 'reject') => {
    if (!a.approval_id || a.approving) return
    // Bound to the request the spawn card was raised for; one that names none
    // has nothing live to decide.
    const target = a.approval_target
    if (!target) {
      endGoneSpawn(a, null)
      return
    }
    dispatch(markSubagentApproving({ id: a.id, approving: true }))
    api.decideApproval(target, action).then(() => {
      // Terminate a rejected card optimistically so the banner does not depend
      // on a WebSocket round trip. The slot-scoped `approval_resolved` frame
      // converges this state idempotently when it arrives. An approved spawn
      // also converges through its spawn/chunk/done stream, while a rejected
      // spawn emits no lifecycle events beyond the resolution frame. The card
      // renders this value verbatim under its error label, so it carries the
      // same catalog sentence the WS retire path uses, not the raw token.
      if (action === 'reject' && slotId) {
        dispatch(sseSubagentDone({ slot: slotId, id: a.id, elapsed: 0, error: i18nT('hooks.useWebSocket.approval_rejected') }))
      }
    }).catch((e: unknown) => {
      dispatch(markSubagentApproving({ id: a.id, approving: false }))
      if (isTerminalApprovalRefusal(e)) endGoneSpawn(a, target)
    })
  }, [dispatch, slotId, endGoneSpawn])
  const resolveSpawnApprovals = useCallback((action: 'approve' | 'reject') => {
    for (const a of pendingSpawnApprovals) resolveOneSpawn(a, action)
  }, [pendingSpawnApprovals, resolveOneSpawn])

  return { pendingSpawnApprovals, reviewSpawnApprovals, spawnApprovalsResolving, resolveOneSpawn, resolveSpawnApprovals }
}
