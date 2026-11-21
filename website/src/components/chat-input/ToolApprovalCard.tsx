import { useCallback, useId, useLayoutEffect, useMemo, useState, type ReactNode } from 'react'
import { motion, useIsPresent } from 'framer-motion'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { COMMAND_WRAP, CommandText, CuedScrollBox, edgeAttrs, edgeMask } from './CuedScrollBox'
import { useRequestRegionCap } from './requestRegionCap'
import { Plug, Target, Terminal } from 'lucide-react'
import DenyOverrideDropdown, { DenyOverrideWarning } from '../DenyOverrideDropdown'
import RejectDropdown from '../RejectDropdown'
import TrustDropdown, { TrustUnavailable } from '../TrustDropdown'
import { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../ApprovalModePicker'
import { FormattedPayloadCard } from '../../pages/chat/ToolDetails'
import { fmtDateFields } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { safeSetItem } from '../../utils/safeStorage'
import { approvalBtnClass, approvalPrimaryBtnClass, type useToolApproval } from './approval'
import { RaisedBy } from './TypingLockBanner'

/* The pending tool approval as a card above the composer, and the one-line
   receipt that replaces it once a decision made here lands. `useToolApproval`
   (./approval) owns the state and the decision; this file only draws it. */

type ApprovalState = 'waiting' | 'approved' | 'rejected'

const STATUS_LABEL_KEY: Record<ApprovalState, string> = {
  // The same words, for the same state, as the transcript's own tool row.
  waiting: 'pages.chat.toolDetails.waiting_for_approval',
  approved: 'components.chatInput.approval_status_approved',
  rejected: 'components.chatInput.approval_status_rejected',
}

const STATUS_CLASS: Record<ApprovalState, string> = {
  waiting: 'bg-[color-mix(in_srgb,var(--warn)_22%,transparent)] text-warn-text',
  approved: 'bg-ok-subtle text-ok-text',
  rejected: 'bg-[color-mix(in_srgb,var(--text)_10%,transparent)] text-muted',
}

/** The shared-layout id one request's waiting pill and its receipt's pill use,
 *  so the pill the user just read moves into the receipt and turns into its
 *  verdict instead of vanishing with the card. Per slot and per request: the
 *  next card's pill never shares a layout stack with the last one's receipt,
 *  which Framer Motion would otherwise animate out of it. */
export const approvalPillLayoutId = (slot: string | null, request: string) => `approval-status-pill:${slot ?? ''}:${request}`

/** One calm timing for the card leaving, the pill moving and the receipt
 *  arriving: slow enough to follow, eased at both ends, never a spring that
 *  overshoots. Under reduced motion none of it runs: the card and the receipt
 *  swap at once (`settleMotion`). */
export const APPROVAL_SETTLE = { duration: 0.45, ease: [0.4, 0, 0.2, 1] } as const

/** Fade props for the card and the receipt, or none when the reader asked for
 *  less motion. `data-motion` names which, so the reduced path is checkable. */
export function settleMotion(reduced: boolean) {
  return reduced
    ? { initial: false as const, animate: { opacity: 1 }, exit: { opacity: 0, transition: { duration: 0 } }, transition: { duration: 0 }, 'data-motion': 'instant' }
    : { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: APPROVAL_SETTLE, 'data-motion': 'settle' }
}

/** The card's fade-in. Its controls take no input until the fade ends: a button
 *  at near-zero opacity is one a person can hit, or reach by keyboard, before
 *  they can read what it allows. `inert` holds both, and is set before the first
 *  paint. Under reduced motion there is no fade, so nothing waits. A card
 *  fading out takes no input either, and one that comes back before its fade-out
 *  ends (the next request lands within it) fades in again and waits again.
 *  So does a queued request that takes the card in place when the one on it is
 *  answered (`requestKey` changes while the card stays mounted): the second
 *  click of a double click must not approve a request nobody has read yet. */
export function ApprovalCardMotion({ reduced, requestKey, children }: { reduced: boolean; requestKey: string; children: ReactNode }) {
  const isPresent = useIsPresent()
  const [settled, setSettled] = useState(reduced)
  const [wasPresent, setWasPresent] = useState(isPresent)
  const [shownKey, setShownKey] = useState(requestKey)
  if (wasPresent !== isPresent) {
    setWasPresent(isPresent)
    if (isPresent) setSettled(reduced)
  }
  if (shownKey !== requestKey) {
    setShownKey(requestKey)
    setSettled(reduced)
  }
  const live = settled && isPresent
  const [el, setEl] = useState<HTMLDivElement | null>(null)
  useLayoutEffect(() => { if (el) el.inert = !live }, [el, live])
  return (
    // Keyed per request, so a request that takes the card in place fades in
    // from the start and its `onAnimationComplete` ends its own hold.
    <motion.div key={requestKey} ref={setEl} data-testid="approval-card-motion" data-settling={live ? undefined : ''} {...settleMotion(reduced)} onAnimationComplete={() => { if (isPresent) setSettled(true) }}>
      {children}
    </motion.div>
  )
}

/** Where the request stands, in words. The dot breathes only while a decision
 *  is still owed, and holds still for a reader who asked for less motion, who
 *  also gets no shared-layout move. */
export function ApprovalStatusPill({ state, layoutId }: { state: ApprovalState; layoutId?: string }) {
  const reduced = useReducedMotion()
  const still = reduced || state !== 'waiting'
  return (
    // Keyed on the id it holds: Framer Motion reads `layoutId` only at mount, so
    // a pill whose request changes under the same card remounts to take its id.
    <motion.span key={reduced ? 'still' : layoutId} layoutId={reduced ? undefined : layoutId} data-layout-id={reduced ? undefined : layoutId} transition={{ layout: APPROVAL_SETTLE }} className={`inline-flex shrink-0 max-w-full items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12px] leading-4 font-body font-medium ${STATUS_CLASS[state]}`}>
      <motion.span
        aria-hidden
        className="size-1.5 rounded-full bg-current"
        animate={still ? { opacity: 1 } : { opacity: [1, 0.3, 1] }}
        transition={still ? { duration: 0 } : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
      />
      {i18nT(STATUS_LABEL_KEY[state])}
    </motion.span>
  )
}

/** What a decision made in this tab did: the verdict as the card's own pill,
 *  then one line on what was granted and when it ends. */
/** `request`: the answered request's key, so the receipt's pill shares a
 *  layout id with that request's card pill and no other. */
export function ApprovalReceipt({ kind, slot, request, children }: { kind: 'granted' | 'rejected'; slot: string | null; request: string; children: ReactNode }) {
  const reduced = useReducedMotion()
  return (
    <motion.div {...settleMotion(reduced)} role="status" data-testid="approval-receipt" className="flex items-center gap-2 flex-wrap px-4 py-2 mb-1 rounded-lg bg-[color-mix(in_srgb,var(--text)_5%,transparent)]">
      <ApprovalStatusPill state={kind === 'granted' ? 'approved' : 'rejected'} layoutId={approvalPillLayoutId(slot, request)} />
      <span className={`min-w-0 text-muted text-[13px] ${COMMAND_WRAP}`}>{children}</span>
    </motion.div>
  )
}

const CHIP_CLASS = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-[11px] leading-4'

/** The card's heading: the title and, with simplified names off, the agent's
 *  description under it, each clamped to two lines. When either is cut, a
 *  "Show more" button (a real button, so Tab and Enter reach it) opens both
 *  whole: the title may be the only place the tool's name shows. Clamping is
 *  measured on every resize, so the button appears only when text is hidden. */
function ApprovalHeading({ title, purpose }: { title: string; purpose: string }) {
  const [expanded, setExpanded] = useState(false)
  const [clamped, setClamped] = useState(false)
  const [box, setBox] = useState<HTMLSpanElement | null>(null)
  const id = useId()
  useLayoutEffect(() => {
    if (!box || expanded) return
    const measure = () => setClamped([...box.querySelectorAll<HTMLElement>('[data-clamp]')].some(el => el.scrollHeight > el.clientHeight + 1))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(box)
    return () => ro.disconnect()
  }, [box, expanded, title, purpose])
  const clamp = expanded ? '' : 'line-clamp-2'
  return (
    // At least 9rem wide, so on a narrow pane it wraps under the status pill
    // instead of collapsing beside it.
    <span ref={setBox} id={id} className="flex-1 min-w-[9rem] pt-px flex flex-col items-start">
      <span data-testid="approval-title" data-clamp="" className={`self-stretch text-[13px] leading-5 font-body font-medium text-text break-words ${clamp}`}>{title}</span>
      {/* Simplified names off: the agent's description of the call, under the label. */}
      {purpose && <span data-testid="approval-purpose" data-clamp="" className={`self-stretch text-[12px] leading-5 font-body text-muted break-words ${clamp}`}>{purpose}</span>}
      {(clamped || expanded) && (
        <button
          type="button"
          data-testid="approval-title-toggle"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(v => !v)}
          className="mt-0.5 p-0 bg-transparent border-0 text-[12px] leading-4 font-body text-accent cursor-pointer hover:underline"
        >
          {i18nT(expanded ? 'components.chatInput.approval_title_less' : 'components.chatInput.approval_title_more')}
        </button>
      )}
    </span>
  )
}

/** The site-grant row's label, per class (#39990). A site grant lasts this session only. */
const BROWSER_SITE_KEYS = {
  read: 'components.trustDropdown.trust_site_read_session',
  act: 'components.trustDropdown.trust_site_act_session',
} as const

export function ToolApprovalCard({ approval, activeSlot, approvalMode }: {
  approval: ReturnType<typeof useToolApproval>
  activeSlot: string | null
  approvalMode?: string
}) {
  const {
    approvalKey, approvalSubmitting, setApprovalPickerSignal, approvalModeAdjusted, approvalNudgeActive,
    approvalToolInput, approvalIsReadOnly, approvalFullCommand, approvalBaseCommand, approvalIsShell, approvalTakesReason,
    approvalTrustCommandGrantable, approvalTrustBaseGrantable, approvalTrustAllGrantable, approvalIsUnattended, approvalTrustGrantable,
    approvalAlwaysAllowRef, approvalAlwaysAllowRefusal, approvalBrowserTrustClass,
    approvalDenyRule, approvalDenyDetail, approvalDenyFamily, approvalDenyCommand,
    approvalToolCallId, approvalTs, approvalTitle, approvalPurposeLine, approvalMcpServer, approvalToolName, approvalRaisedBy, showGhost, showInChat, handleApprovalAction, approvalActionsRef,
  } = approval
  const siteKey = approvalBrowserTrustClass ? BROWSER_SITE_KEYS[approvalBrowserTrustClass] : undefined
  // The exact command Allow once runs is read here, so the box fades at the
  // edge that hides text (a clean cut at a newline never reads as the end of
  // the command), and is keyed per request, so the next request opens at its
  // own first line with its edges measured afresh.
  const [attachCommandEdges, commandEdges] = useScrollEdgesY<HTMLPreElement>()
  // The request region (kind chips, command, arguments) is one scroller capped
  // so the card never rises past the pane's top or under the title overlay
  // (requestRegionCap.ts); it cues whatever the cap hides.
  const [cardEl, setCardEl] = useState<HTMLDivElement | null>(null)
  const [regionEl, setRegionEl] = useState<HTMLDivElement | null>(null)
  const [attachRegionEdges, regionEdges, , attachRegionContent] = useScrollEdgesY<HTMLDivElement>()
  const setRegion = useCallback((node: HTMLDivElement | null) => { setRegionEl(node); attachRegionEdges(node) }, [attachRegionEdges])
  const regionCap = useRequestRegionCap(cardEl, regionEl)
  // Too little room even for the region's floor: part of the request is past
  // the pane's top or under the title overlay, where no edge cue reaches. The
  // controls that approve hold until the room returns, and say why; Reject,
  // which runs nothing, stays.
  const cramped = regionCap?.cramped ?? false
  const crampedId = useId()

  const kindLabel = approvalIsShell
    ? i18nT('components.chatInput.approval_kind_shell')
    : approvalMcpServer ? i18nT('components.chatInput.approval_kind_mcp', { server: approvalMcpServer }) : ''
  const KindIcon = approvalIsShell ? Terminal : Plug
  // A shell call is named by its command box; any other tool by its own name,
  // since the heading above may be the agent's description of the call. Not
  // repeated when the heading already is that name.
  const toolName = approvalIsShell || approvalToolName === approvalTitle ? '' : approvalToolName
  const canShowInChat = showGhost && !!approvalToolCallId
  // An override card states the refused command in its own warning below.
  // A browser site grant's `full_command` is the site's host, the grant's
  // scope, not what Allow once runs: the call itself is its input, below, and
  // the Trust row names the host.
  const command = approvalDenyRule || siteKey ? '' : approvalFullCommand
  // The string the card already shows whole, in the command box or the
  // override warning, and so leaves out of the payload.
  const shownCommand = approvalDenyRule ? approvalDenyCommand : command
  const commandKey = i18nT(approvalIsShell ? 'components.chatInput.approval_command_key' : 'components.chatInput.approval_call_key')
  // A shell call's command is already in the command box, so it is not printed
  // again; whatever else the call carries (a working directory, a timeout) is
  // still shown, since it changes what the command does. Any other input (a
  // tool's arguments, a command the gateway could not give a grantable form)
  // is shown as the transcript shows it.
  const payload = useMemo(() => {
    if (!approvalIsShell || !shownCommand) return approvalToolInput
    // Input the card cannot split shows as it came, unless it is only the command.
    const whole = approvalToolInput.trim() === shownCommand.trim() ? '' : approvalToolInput
    try {
      const parsed: unknown = JSON.parse(approvalToolInput)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return whole
      // Only the `command` field, the one the gateway reads the command from,
      // is dropped, and only when it is the string shown. Every other field
      // stays, even one whose value happens to equal the command (a `cwd` of
      // `make` beside `make`): it changes what the command does.
      const rest = Object.entries(parsed).filter(([key, value]) => !(key === 'command' && typeof value === 'string' && value.trim() === shownCommand.trim()))
      return rest.length > 0 ? JSON.stringify(Object.fromEntries(rest)) : ''
    } catch {
      // Not JSON: shown as it came, unless it is only the command.
      return whole
    }
  }, [approvalIsShell, shownCommand, approvalToolInput])
  // The call every Trust row and override row also approves, as shown above:
  // the menus that open over this region print it themselves.
  // A call with no command is named by its kind and tool chips, also under the menu.
  const pendingCall = { command: shownCommand ?? '', input: payload, tool: { kind: kindLabel, name: approvalToolName } }

  return (
    <div ref={setCardEl} className={`bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] ${showGhost ? 'rounded-2xl' : 'rounded-t-2xl border-b border-[color:var(--glass-edge)]'} transition-[border-radius,border-color,border-width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`}>
      {/* Pill, heading and (on a Shared Crew) who raised it, on one row where
          they fit. On a narrow pane the heading drops under the pill rather
          than shrinking to nothing, and the attribution takes its own line. */}
      <div data-testid="approval-header" className="flex flex-wrap items-start gap-x-2 gap-y-1 px-3.5 pt-2.5">
        <ApprovalStatusPill state="waiting" layoutId={approvalPillLayoutId(activeSlot, approvalKey)} />
        {/* Prose, so the body face: the command itself is below, in the one
            place it is set in mono. */}
        {/* Keyed per request, so the next one opens clamped again. */}
        <ApprovalHeading key={approvalKey} title={approvalTitle} purpose={approvalPurposeLine} />
        <RaisedBy name={approvalRaisedBy} />
      </div>
      <div
        // Per request, so the next one opens at its top with its edges measured afresh.
        key={approvalKey}
        ref={setRegion}
        data-testid="approval-request-region"
        {...edgeAttrs(regionEdges)}
        // 40svh, or less where the pane is shorter: see requestRegionCap.ts.
        style={{ maxHeight: regionCap === null ? '40svh' : `min(40svh, ${regionCap.cap}px)` }}
        className={`px-3.5 overflow-y-auto ${edgeMask(regionEdges)}`}
      >
        <div ref={attachRegionContent} className="mt-2 mb-2.5 ml-1.5 border-l-2 pl-3 flex flex-col gap-2" style={{ borderLeftColor: 'color-mix(in srgb, var(--warn) 70%, transparent)' }}>
          {(kindLabel || toolName || approvalIsReadOnly || approvalTs > 0 || canShowInChat) && (
            <div className="flex items-center gap-2 flex-wrap select-none">
              {kindLabel && (
                // The server name is the gateway's, whole: a long one wraps
                // inside the chip instead of running past the card.
                <span data-testid="approval-kind" className={`${CHIP_CLASS} min-w-0 max-w-full font-body font-medium text-text`}>
                  <KindIcon size={11} className="shrink-0 text-muted" /><span className={`min-w-0 ${COMMAND_WRAP}`}>{kindLabel}</span>
                </span>
              )}
              {toolName && <span data-testid="approval-tool" className={`${CHIP_CLASS} min-w-0 max-w-full font-mono text-text ${COMMAND_WRAP}`}>{toolName}</span>}
              {approvalIsReadOnly && (
                <span data-testid="approval-read-only" className="inline-flex items-center px-2 py-0.5 rounded-md border border-[color-mix(in_srgb,var(--ok)_30%,transparent)] bg-ok-subtle text-ok-text text-[11px] leading-4 font-body font-medium">
                  {i18nT('components.chatInput.approval_read_only')}
                </span>
              )}
              {approvalTs > 0 && <span className={`${CHIP_CLASS} font-mono text-muted`}>{fmtDateFields(approvalTs, { hour: '2-digit', minute: '2-digit' })}</span>}
              {/* Only while the transcript's own row for this call is out of
                  view. Here and not in the action row, which holds its three. */}
              {canShowInChat && (
                <button
                  type="button"
                  onClick={showInChat}
                  title={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                  aria-label={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                  // A live control at the weight of the card's other buttons;
                  // muted text read as switched off (UX review).
                  className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-bg-elevated border border-border-strong text-text font-body font-medium text-[11px] leading-4 cursor-pointer hover:bg-bg-hover transition-colors"
                >
                  <Target size={11} className="shrink-0" />
                  {i18nT('components.chatInput.show_in_chat')}
                </button>
              )}
            </div>
          )}
          {command && (
            <div className="flex gap-3 rounded-md border border-border bg-bg-elevated px-2.5 py-2 text-[12px] leading-5 font-mono">
              <span className={`shrink-0 max-w-[40%] select-none ${COMMAND_WRAP}`} style={{ color: 'var(--json-key)' }}>{commandKey}</span>
              {/* THE WHOLE COMMAND, AS SENT: this is where the string Allow once
                  runs is read (the Trust menu's exact-command row prints it
                  too). Hence
                  no shortening and no highlighter between the string and the
                  reader; `whitespace-pre-wrap` because collapsing a run of
                  spaces shows a different command than the one granted (a
                  space at a soft wrap hangs past the line's end rather than
                  starting the next line indented), and `COMMAND_WRAP` with
                  `CommandText` so it wraps at spaces and splits only a token
                  too long for the line (a sha, a base64 argument) rather than
                  widening the card. Bounded and scrollable,
                  as the override card's command is: a pathological command must
                  not push the buttons out of the pane. */}
              {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact command. */}
              <pre key={approvalKey} ref={attachCommandEdges} tabIndex={0} role="region" aria-label={commandKey} data-testid="approval-command" {...edgeAttrs(commandEdges)} className={`m-0 flex-1 min-w-0 max-h-[30vh] overflow-y-auto whitespace-pre-wrap ${COMMAND_WRAP} font-mono text-text ${edgeMask(commandEdges)}`}><CommandText text={command} /></pre>
            </div>
          )}
          {payload && (
            <CuedScrollBox key={approvalKey} className="max-h-[160px] [&>div]:my-0" testId="approval-input" label={i18nT('components.chatInput.approval_input_label')}>
              <FormattedPayloadCard text={payload}>
                <pre className={`m-0 px-2 py-2 rounded-md border border-border bg-bg-elevated text-[12px] leading-5 font-mono whitespace-pre-wrap ${COMMAND_WRAP}`}><CommandText text={payload} /></pre>
              </FormattedPayloadCard>
            </CuedScrollBox>
          )}
        </div>
      </div>
      {/* `data-approval-actions` is the probe `queryPendingApprovalAction`
          resolves, and the reject chord's event arrives on the row. A `data-`
          hook rather than button text because every catalog translates the labels.
          Each menu here is `modal`: it opens over the request, so on touch an
          outside tap only closes it and never also lands on Allow once. */}
      {cramped && <p id={crampedId} role="status" data-testid="approval-cramped" className="m-0 px-3.5 pb-1.5 text-[12px] text-warn-text select-none">{i18nT('components.chatInput.approval_cramped')}</p>}
      <div ref={approvalActionsRef} data-approval-actions className="flex gap-1.5 flex-wrap items-center justify-end px-3.5 pb-2.5 select-none">
        {approvalDenyRule ? (
          // Keyed per card row, as Trust and Reject are: a menu left open never acts for the next request.
          <DenyOverrideDropdown key={`override:${activeSlot}:${approvalKey}`} modal disabled={approvalSubmitting || cramped} className={approvalBtnClass} family={approvalDenyFamily} pendingCall={pendingCall} onAction={(action) => { handleApprovalAction(action) }} />
        ) : null}
        {/* Keyed per card row: a typed reason never reaches another card. */}
        <RejectDropdown
          key={`${activeSlot}:${approvalKey}`}
          modal disabled={approvalSubmitting} takesReason={approvalTakesReason}
          className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`}
          onAction={(action, reason) => { handleApprovalAction(action, undefined, undefined, reason) }}
        />
        {!approvalDenyRule && <>
          {/* One dropdown carries every standing grant (trust-reads is a tier in it), and a card with none
              keeps its place disabled: `max-two-buttons-per-row` grandfathers Reject + Trust + Allow once; no fourth. */}
          {approvalTrustGrantable && (approvalTrustCommandGrantable || approvalTrustAllGrantable || approvalIsReadOnly || !!approvalAlwaysAllowRef || !!approvalAlwaysAllowRefusal) ? (
            <TrustDropdown
              // Keyed per card row, as Reject is: an armed confirmation or a
              // chosen lifetime belongs to this request and never reaches another.
              key={`${activeSlot}:${approvalKey}`}
              fullCommand={approvalFullCommand}
              baseCommand={approvalBaseCommand}
              isShell={approvalIsShell && approvalTrustBaseGrantable}
              hasCommand={approvalTrustCommandGrantable}
              // A browser site grant: the command row names the site and what is
              // trusted on it, and has one lifetime, so it keeps the plain menu.
              trustCommandLabelKey={siteKey}
              confirmTrustAll
              trustReadsLabelKey={approvalIsReadOnly ? 'components.chatInput.trust_reads' : undefined}
              showTrustAll={approvalTrustAllGrantable || approvalTrustCommandGrantable}
              disabled={approvalSubmitting || cramped}
              className={approvalBtnClass}
              timedGrants={!siteKey}
              modal
              pendingCall={pendingCall}
              alwaysAllowTool={approvalAlwaysAllowRef}
              alwaysAllowRefusal={approvalAlwaysAllowRefusal}
              commandNoun={approvalIsShell ? 'command' : 'call'}
              onAction={(action, pattern, ttlSeconds) => { handleApprovalAction(action, pattern, ttlSeconds) }}
            />
          ) : <TrustUnavailable className={approvalBtnClass} />}
          {/* The one filled control, last in the row and so nearest the
              composer. `data-approval-primary` is where the focus-approval
              chord lands; a trust grant is never one keystroke from it. */}
          <button data-approval-primary disabled={approvalSubmitting || cramped} aria-describedby={cramped ? crampedId : undefined} className={approvalPrimaryBtnClass} onClick={() => handleApprovalAction('approved')}>{i18nT('components.chatInput.allow_once')}</button>
        </>}
      </div>
      {approvalDenyRule && <DenyOverrideWarning rule={approvalDenyRule} family={approvalDenyFamily} detail={approvalDenyDetail} command={approvalDenyCommand} />}
      {/* A1 hint: points at the footer mode picker. Withheld for unattended
          sources (the picker governs THIS slot), the ghost state (picker
          unmounted), an override card, the B2 nudge, and once the user has
          found the picker (this link or a mode change). */}
      {!showGhost && !approvalIsUnattended && !approvalDenyRule && !approvalModeAdjusted && !approvalNudgeActive && approvalMode && (
        <div className="flex items-center gap-1.5 flex-wrap px-3.5 pb-2 -mt-1 text-[12px] text-muted select-none">
          <span>{i18nT('components.chatInput.approval_hint_question')}</span>
          <button
            type="button"
            className="inline-flex items-center gap-0.5 p-0 bg-transparent border-none text-accent text-[12px] cursor-pointer hover:underline"
            onClick={() => {
              // Discovery achieved: the picker is about to open under a
              // spotlight, so the hint has done its job for good.
              safeSetItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
              setApprovalPickerSignal(n => n + 1)
            }}
          >
            {i18nT('components.chatInput.approval_hint_adjust')}
          </button>
        </div>
      )}
    </div>
  )
}
