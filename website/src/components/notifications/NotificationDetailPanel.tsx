import { useEffect, useMemo, useRef, useState } from 'react'
import { X, MailOpen, Check, MessageSquare, CheckCircle, Ban, Clock, ClipboardList, ArrowUpRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useGuardedLeave } from '../NavigationLeaveGuard'
import { useAppSelector, useAppDispatch } from '../../store'
import { ackNotification, unackNotification, decideApprovalRow, dismissNotificationRow } from '../../store/notificationsSlice'
import { switchSlot, resumeFromHistory } from '../../store/chatSlice'
import { Badge } from '../ui'
import MarkdownRenderer from '../MarkdownRenderer'
import MessageErrorBoundary from '../MessageErrorBoundary'
import ErrorNotice from '../ErrorNotice'
import StatusNotice from '../StatusNotice'
import { CronAckBar } from '../../pages/chat'
import { api } from '../../api/client'
import type { Notification } from '../../types'
import { KIND_META, DEFAULT_META, fmtFull, safeInternalUrl, decisionFailedCopy, endedDecisionFailedCopy } from './notifMeta'
import { CrewmateNoteFace, CrewmateNoteName, CrewmateRosterError, noteMember } from './CrewmateNoteFace'
import { safeHttpUrl } from '../../lib/safeUrl'

import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
/** Intentional failure diagnostic for the navigation/approval actions below. */
function logError(msg: string, err: unknown): void {
  // eslint-disable-next-line no-console -- intentional failure diagnostic
  console.error(msg, err)
}

/**
 * Notification detail view. Takes dispatch/navigate from hooks (not props) so it
 * renders identically whether hosted in the full page (right column) or the
 * topbar bell popover (slide-out).
 */
export default function NotificationDetailPanel({ n, onClose }: { n: Notification; onClose: () => void }) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  // Every jump below runs inside this gate, and the gate asks BEFORE the
  // handler: the panel is opened over whatever page the user was on — including
  // one holding an unsaved draft — and most of these handlers switch a slot,
  // resume a session, or close this panel on their way out. Vetoing only the
  // final `navigate` would leave all of that applied.
  const leave = useGuardedLeave()
  // A failed "open in chat" click, made visible (#12557). It holds only WHICH
  // notification failed: the server's own words ("to-chat returned 500") name a
  // mechanism and nothing to do about it, so they stay in the console. The id
  // scopes it because the bell popover reuses one instance across rows (no
  // `key`, unlike the page), so an unscoped error would follow the reader.
  const [handoffError, setHandoffError] = useState<string | null>(null)
  const handoffFailed = handoffError === n.ts
  // A retryable decision failure (the approval may still be pending, so the
  // buttons stay), scoped to the notification it belongs to for the same
  // panel-reuse reason as `handoffError`. `reason` is the server's own refusal
  // text ('' for a response-less transport failure, which gets the hedged
  // copy), as in `ApprovalCard`.
  const [decideFailure, setDecideFailure] = useState<{ ts: string; reason: string } | null>(null)
  const failureHere = decideFailure?.ts === n.ts ? decideFailure : null
  // A terminal refusal lives in the notifications slice, shared with the page
  // feed and the bell popover: it only RETIRES the row (it stays until
  // dismissed), so a remounted or reused panel reads the same state as every
  // other view.
  // A press that failed after a frame ended its request retires the row the
  // same way, carrying that failure (`{ failed }`) to show beside it.
  const retiredMark = useAppSelector(s => s.notifications.retiredApprovals?.[n.ts])
  const retired = retiredMark !== undefined
  const endedFailure = typeof retiredMark === 'object' ? retiredMark.failed : null
  // A decision that landed on a stored note whose DELETE then failed: the
  // controls are withdrawn as for a refusal, and the notice says why the row
  // stayed. Only an approval row carries that notice, so only an approval row
  // withdraws its controls for it: a cron or info note keeps its read toggle
  // and its Close, as the feed's `settled` gate does.
  const dismissFailed = useAppSelector(s => s.notifications.dismissFailed?.[n.ts])
  const buttonsWithdrawn = retired || (n.kind === 'approval' && !!dismissFailed)
  // A retired row made no decision, so its failed DELETE is a plain dismiss
  // failure, never "decision recorded"; the feed reads the same rule.
  const decidedDismissFailed = !retired && dismissFailed === 'decided'
  const refusalCopy = failureHere && !buttonsWithdrawn ? decisionFailedCopy(failureHere)
    : endedFailure !== null ? endedDecisionFailedCopy(endedFailure) : null
  // Withdrawing the buttons unmounts the one the user just pressed, which
  // drops keyboard focus to <body>. Move it to the panel's Close, which only
  // closes the panel and never removes anything, so a keyboard user pressing
  // Enter there cannot delete the row by accident. The destructive Dismiss
  // sits next to the notice and is never focused for the user. Only when focus
  // was actually lost: a reader who has moved on keeps their place.
  const refusalRef = useRef<HTMLDivElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (!buttonsWithdrawn) return
    // The notice is the point of the withdrawal: bring all of it into view.
    refusalRef.current?.scrollIntoView?.({ block: 'nearest' })
    const active = document.activeElement
    if (active && active !== document.body) return
    closeRef.current?.focus({ preventScroll: true })
  }, [buttonsWithdrawn])
  // `decideApprovalRow` holds the row through the response and awaits its
  // removal, so the panel closes only once the row has really left. A refusal
  // retires the row and a failed DELETE marks it `dismissFailed`; both keep
  // the panel open with the controls withdrawn.
  const decide = async (action: 'approve' | 'reject') => {
    setDecideFailure(null)
    const result = await dispatch(decideApprovalRow({ n, action }))
    if (!decideApprovalRow.fulfilled.match(result)) return
    const outcome = result.payload
    if (outcome.kind === 'landed') onClose()
    else if (outcome.kind === 'failed') setDecideFailure({ ts: n.ts, reason: outcome.reason })
  }
  // Close always just closes the panel. A withdrawn row is taken out of the
  // feed by its own Dismiss next to the notice (as its close X in the feed
  // does). A DELETE that fails again leaves the row in the feed with its notice.
  // The pressed Dismiss unmounts with the panel, so keyboard focus moves to the
  // feed's list (focusable, tabIndex -1) instead of falling to <body>. Scoped
  // to the host this panel sits in (the bell sheet or the page's split, each
  // marked data-notif-scope), so a panel in the sheet never focuses the page's
  // list behind it. Deferred past the close, because on mobile the page hides
  // the feed column while a note is selected and only shows it again once the
  // close has re-rendered; focus() on a hidden element does nothing. Only if
  // focus was actually lost, as in the rescue above: a click that never moved
  // focus (Safari, Firefox on macOS) leaves the reader where they were.
  const rootRef = useRef<HTMLDivElement | null>(null)
  const dismissRow = () => {
    void dispatch(dismissNotificationRow(n))
    const scope = rootRef.current?.closest('[data-notif-scope]')
    onClose()
    requestAnimationFrame(() => {
      const active = document.activeElement
      if (active && active !== document.body) return
      scope?.querySelector<HTMLElement>('[data-notif-list]')?.focus({ preventScroll: true })
    })
  }
  const km = KIND_META[n.kind] || DEFAULT_META
  const member = noteMember(n)
  const slots = useAppSelector(s => s.dashboard.slots)

  // The sentence names the button BY ITS OWN LABEL, interpolated rather than
  // written into each catalog, so it cannot drift from the control it points at.
  // Two sentences, because the two sites do different things: the cron button is
  // called "View last result" and a reader hearing "could not open the chat"
  // there said nothing on the screen is even called a chat. Only one of the two
  // buttons is ever mounted (they are keyed on `n.kind`), so the kind picks it.
  //
  // No `onDismiss`: its button would be a THIRD action in a row that already
  // holds two (`max-two-buttons-per-row`), and nothing needs it -- the notice
  // clears on the next click, and the id drops it when the reader moves on.
  const handoffFailure = n.kind === 'cron'
    ? i18nT('components.notifications.notificationDetailPanel.could_not_open_the_result_in_chat_click_button_to_retry', {
      button: i18nT('components.notifications.notificationDetailPanel.view_last_result'),
    })
    : i18nT('components.notifications.notificationDetailPanel.could_not_open_the_chat_click_button_to_retry', {
      button: i18nT('components.notifications.notificationDetailPanel.continue_in_chat'),
    })
  const handoffNotice = (
    /* No hand-off: this panel is an OVERLAY over whatever page the reader was
       on, including one holding an unsaved draft -- which is why every action
       here runs inside `useGuardedLeave`. The hand-off navigates to the chat
       directly, bypassing that gate, so it would discard the host page's draft
       without asking: the exact loss that guard exists to prevent. The sentence
       carries its own remedy, and the button that failed is still in this row. */
    <ErrorNotice
      variant="inline"
      message={handoffFailed ? handoffFailure : null}
      testId="notif-handoff-error"
    />
  )

  // Direct slot link from notification meta
  const directSlot = n.slot ? slots.find(s => s.key === n.slot) : null

  // Fuzzy match: try to find a related chat slot from the body/title
  const relatedSlot = useMemo(() => {
    if (directSlot) return null  // prefer direct
    for (const s of slots) {
      if (s.title && s.title.length >= 4 && n.body) {
        const re = new RegExp(`\\b${s.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i')
        if (re.test(n.body)) return s
      }
      if (n.title?.includes(s.key)) return s
    }
    return null
  }, [slots, n, directSlot])

  return (
    <div ref={rootRef} className="flex flex-col h-full border-l border-border bg-bg">
      {/* Header */}
      <div className="px-5 py-3 border-b border-border flex items-center justify-between bg-chrome shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          {member
            ? <CrewmateNoteFace member={member} size={22} fallback={<span className="text-[16px]">{km.icon}</span>} />
            : <span className="text-[16px]">{km.icon}</span>}
          <span className="text-sm font-semibold text-text-strong truncate">{n.title}</span>
        </div>
        <button ref={closeRef} data-testid="notif-detail-close" className="text-muted text-[13px] cursor-pointer hover:text-text bg-transparent border-none font-body shrink-0 ml-2" onClick={onClose}><X className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.close')}</button>
      </div>

      {/* Meta bar */}
      <div className="px-5 py-3 border-b border-border flex items-center gap-3 flex-wrap bg-bg-elevated shrink-0">
        <span className={`px-2 py-[3px] rounded-full text-[12px] font-bold ${km.color} border border-[color-mix(in_srgb,currentColor_20%,transparent)]`}>{km.label}</span>
        {member && <CrewmateNoteName member={member} className="text-[13px] font-semibold text-text" />}
        <span className="text-[13px] text-muted font-mono">{fmtFull(n.ts)}</span>
        {n.acked
          ? <Badge variant="ok">{i18nT('components.notifications.notificationDetailPanel.read')}</Badge>
          : <Badge variant="warn">{i18nT('components.notifications.notificationDetailPanel.unread')}</Badge>
        }
        {/* A retired approval is read and asks for nothing: marking it
            unread would only relight the badges for a row with nothing to
            decide. */}
        {buttonsWithdrawn ? null : n.acked
          ? <button className="text-[13px] text-muted cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(unackNotification(n.ts))} {...uiLocation('notifications.detail.mark-unread')}><MailOpen className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.mark_unread')}</button>
          : <button className="text-[13px] text-ok cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(ackNotification(n.ts))}><Check className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.mark_read')}</button>
        }
        {member && <CrewmateRosterError className="basis-full" />}
      </div>

      {/* Kind & navigation. The row prints the note's KIND (`km.label`, the same
          noun the `kind_*` catalog keys and `n.kind` use), so it is labelled as
          such: an approval body carries its own "Source:" line naming the system
          that asked, and a reader must not take the two for one field. */}
      <div className="px-5 py-2.5 border-b border-border flex items-center gap-2 flex-wrap shrink-0">
        <span className="text-[12px] text-muted uppercase tracking-[.04em] font-medium">{i18nT('pages.artifactsPage.kind')}</span>
        <span className="text-[13px] text-text">{km.label}{n.kind === 'cron' && n.job_id ? ` (${n.job_id.slice(0, 8)})` : n.kind === 'taskrunner' && n.task_id ? ` (${n.task_id.slice(0, 8)})` : (directSlot || relatedSlot) ? ` · ${(directSlot || relatedSlot)!.title || (directSlot || relatedSlot)!.key}` : ''}</span>
        <span className="flex-1" />
        {/* Jump-to buttons */}
        {n.kind === 'cron' && (
          <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => leave(() => navigate('/schedule'))}><Clock className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_cron_jobs')}</button>
        )}
        {n.kind === 'cron' && n.job_id && n.slot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(() => { dispatch(switchSlot({ key: n.slot!, announceOnMissing: true })); navigate('/chat') })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.go_to_chat')}</button>
        )}
        {n.kind === 'cron' && n.job_id && !n.slot && (<>
          {/* The switch is deliberately NOT awaited into this notice. The
              sentence invites a retry, and `/to-chat` is not idempotent -- it
              mints a fresh task-review slot and spawns another agent told to edit
              the run's work_dir directly. So the notice may only appear where the
              hand-off did NOT succeed; a slot fetch that fails after it did stays
              with the plain-site convention, explained at /chat. */}
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(async () => { setHandoffError(null); try { const res = await api.cronToChat(n.job_id!); if (res.error || !res.slot) { logError('cronToChat failed', res.error); setHandoffError(n.ts); return } dispatch(switchSlot(res.slot)); navigate('/chat') } catch (e) { logError('cronToChat failed', e); setHandoffError(n.ts) } })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_last_result')}</button>
          {/* `inline`, not `block`: this row is a flex line of buttons, and a
              bordered banner dropped into one breaks the row. */}
          {handoffNotice}
        </>)}
        {directSlot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(() => { dispatch(switchSlot({ key: directSlot.key, announceOnMissing: true })); navigate('/chat') })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.go_to_chat')}</button>
        )}
        {!directSlot && n.slot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          /* `.unwrap()` is load-bearing for the diagnostic: a thunk dispatch
             promise RESOLVES even when the thunk rejected, so without it the
             catch below could never run. It does NOT gate the navigation, and
             deliberately so -- /chat is where the slice's outcome notice
             renders, so a resume that failed OR landed on a surface the chat
             page cannot display is EXPLAINED there. Staying put would leave the
             button looking dead, which is the defect this touches (#5925). */
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(async () => { try { await dispatch(resumeFromHistory({ key: n.slot!, title: n.title })).unwrap() } catch (e) { logError('Resume failed', e) } navigate('/chat') })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.resume_chat')}</button>
        )}
        {!directSlot && !n.slot && relatedSlot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(() => { dispatch(switchSlot({ key: relatedSlot.key, announceOnMissing: true })); navigate('/chat') })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.go_to_chat')}</button>
        )}
        {safeHttpUrl(n.slack_link ?? '') && (
          <a href={safeHttpUrl(n.slack_link ?? '')!} target="_blank" rel="noopener noreferrer" className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body no-underline inline-flex items-center gap-1"><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.open_in_slack')}</a>
        )}
        {/* RFC Phase 4: dashboard-internal deep link (validated path-only). */}
        {safeInternalUrl(n.url) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(() => navigate(safeInternalUrl(n.url)!), safeInternalUrl(n.url)!)}><ArrowUpRight className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.open')}</button>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="msg-content bg-card border border-border rounded-lg px-5 py-4 text-sm leading-relaxed text-text shadow-[inset_0_1px_0_var(--card-hl)] max-w-[820px] overflow-x-auto break-words">
          {/* Per-item boundary: a body that crashes the markdown renderer
              degrades the body alone; the header and actions stay usable
              instead of the whole panel escalating to the app ErrorBoundary. */}
          <MessageErrorBoundary rawContent={typeof n.body === 'string' ? n.body : ''}>
            <MarkdownRenderer content={typeof n.body === 'string' ? n.body : ''} readOnlyCode />
          </MessageErrorBoundary>
        </div>

        {/* Kind-specific actions */}
        {n.kind === 'approval' && (
          <>
            {!buttonsWithdrawn && (
              <div className="flex gap-3 mt-4">
                <button className="px-4 py-2 rounded-lg bg-ok text-ok-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => decide('approve')}><CheckCircle className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.approve')}</button>
                <button className="px-4 py-2 rounded-lg bg-danger text-danger-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => decide('reject')}><Ban className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.reject')}</button>
              </div>
            )}
            {/* No hand-off: in the bell popover this panel is an OVERLAY
                that stays open over the page beneath it, such as an unsaved
                prompt edit in the Overview Prompts tab's editor. The hand-off
                navigates to the chat without the `useGuardedLeave` gate, so it
                would unmount that editor and discard the edit. */}
            {/* Scrolled into view when the pressed button is withdrawn; see
                `refusalRef`. */}
            <div ref={refusalRef} data-testid="notif-approval-refusal-focus">
              {/* A failed press leads, then "no longer pending", in the same
                  order as the feed row. */}
              <ErrorNotice
                variant="inline"
                className="mt-4"
                testId="notif-approval-refusal"
                message={refusalCopy}
              />
              {/* A refused decide failed, so it is an error; see the no
                  hand-off note above. */}
              <ErrorNotice
                variant="inline"
                className={refusalCopy !== null ? 'mt-2' : 'mt-4'}
                testId="notif-approval-retired"
                message={retired ? i18nT('components.approvalCard.approval_no_longer_pending') : null}
              />
              {/* A decision that landed is not a failure, so it leads as a
                  neutral status. The clean-up DELETE that failed after it is,
                  so it renders beneath through the shared ErrorNotice (no
                  hand-off, see above). */}
              <StatusNotice
                tone="info"
                className="mt-4"
                testId="notif-decision-recorded"
                message={decidedDismissFailed ? i18nT('components.notifications.notificationFeed.decision_recorded') : null}
              />
              {/* Boxed beside the boxed StatusNotice above it, so the two
                  read as one pair; a plain dismiss failure stays inline, and
                  on a retired row it sits under "no longer pending". */}
              <ErrorNotice
                variant={decidedDismissFailed ? 'block' : 'inline'}
                className={decidedDismissFailed ? 'mt-1' : retired ? 'mt-2' : 'mt-4'}
                testId="notif-dismiss-failed"
                message={!dismissFailed ? null : i18nT(decidedDismissFailed
                  ? 'components.notifications.notificationFeed.decided_clear_failed'
                  : 'components.notifications.notificationFeed.dismiss_failed')}
              />

              {/* Its own line: an inline notice short enough to leave room
                  would otherwise pull the button up beside its text. */}
              {buttonsWithdrawn && (
                <div>
                  <button
                    type="button"
                    data-testid="notif-detail-dismiss"
                    className="mt-3 px-3 py-1.5 rounded-lg border border-border bg-transparent text-text text-[13px] cursor-pointer hover:text-danger transition-colors font-body"
                    onClick={dismissRow}
                  >
                    {i18nT('components.notifications.notificationFeed.dismiss_notification')}
                  </button>
                </div>
              )}
            </div>
          </>
        )}
        {/* RFC Phase 4: generic actions -- rendered only with a validated
            dashboard-internal url (action identifiers, never executable
            content). Array.isArray + string-type checks guard legacy/corrupted
            persisted rows (a truthy non-array `actions` would throw on .filter). */}
        {(() => {
          const urlActions = (Array.isArray(n.actions) ? n.actions : [])
            .filter(a => typeof a?.id === 'string' && typeof a?.label === 'string' && typeof a?.url === 'string' && safeInternalUrl(a.url))
          return urlActions.length > 0 && (
            <div className="flex gap-3 mt-4 flex-wrap">
              {urlActions.map(a => (
                <button key={a.id} className="px-4 py-2 rounded-lg border border-accent/40 bg-transparent text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle transition-all font-body" onClick={() => leave(() => { navigate(safeInternalUrl(a.url)!); onClose() }, safeInternalUrl(a.url)!)}>{a.label}</button>
              ))}
            </div>
          )
        })()}
        {n.kind === 'cron' && n.job_id && (
          <CronAckBar key={n.ts} notification={n} onDone={onClose} />
        )}
        {n.kind === 'taskrunner' && n.task_id && (
          <div className="flex flex-wrap items-center gap-3 mt-4">
            <button className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => leave(async () => { setHandoffError(null); try { const res = await api.taskRunToChat(n.task_id!); if (res.error || !res.slot) { logError('Task nav failed', res.error); setHandoffError(n.ts); return } dispatch(switchSlot(res.slot)); navigate('/chat') } catch (e) { logError('Task nav failed', e); setHandoffError(n.ts) } })}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.continue_in_chat')}</button>
            <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => leave(() => navigate('/projects'))}><ClipboardList className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_project')}</button>
            {handoffNotice}
          </div>
        )}
      </div>
    </div>
  )
}
