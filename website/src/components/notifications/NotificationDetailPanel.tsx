import { useMemo } from 'react'
import { X, MailOpen, Check, MessageSquare, CheckCircle, Ban, Clock, ClipboardList, ArrowUpRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from '../../store'
import { deleteNotification, ackNotification, unackNotification } from '../../store/notificationsSlice'
import { switchSlot, resumeFromHistory } from '../../store/chatSlice'
import { Badge } from '../ui'
import MarkdownRenderer from '../MarkdownRenderer'
import { CronAckBar } from '../../pages/chat'
import { api } from '../../api/client'
import type { Notification } from '../../types'
import { KIND_META, DEFAULT_META, fmtFull, safeInternalUrl } from './notifMeta'
import { safeHttpUrl } from '../../lib/safeUrl'

import { i18nT } from '../../i18n/t'
/** Intentional failure diagnostic for the navigation/approval actions below. */
function logError(msg: string, err: unknown): void {
  // eslint-disable-next-line no-console -- intentional failure diagnostic
  console.error(msg, err)
}

/**
 * Notification detail view. Extracted verbatim from NotificationsPage's
 * DetailPanel; dispatch/navigate now come from hooks (instead of props) so it
 * renders identically whether hosted in the full page (right column) or the
 * topbar bell popover (slide-out).
 */
export default function NotificationDetailPanel({ n, onClose }: { n: Notification; onClose: () => void }) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const km = KIND_META[n.kind] || DEFAULT_META
  const slots = useAppSelector(s => s.dashboard.slots)

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
    <div className="flex flex-col h-full border-l border-border bg-bg">
      {/* Header */}
      <div className="px-5 py-3 border-b border-border flex items-center justify-between bg-chrome shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[16px]">{km.icon}</span>
          <span className="text-sm font-semibold text-text-strong truncate">{n.title}</span>
        </div>
        <button className="text-muted text-[13px] cursor-pointer hover:text-text bg-transparent border-none font-body shrink-0 ml-2" onClick={onClose}><X className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.close')}</button>
      </div>

      {/* Meta bar */}
      <div className="px-5 py-3 border-b border-border flex items-center gap-3 flex-wrap bg-bg-elevated shrink-0">
        <span className={`px-2 py-[3px] rounded-full text-[12px] font-bold ${km.color} border border-current/20`}>{km.label}</span>
        <span className="text-[13px] text-muted font-mono">{fmtFull(n.ts)}</span>
        {n.acked
          ? <Badge variant="ok">{i18nT('components.notifications.notificationDetailPanel.read')}</Badge>
          : <Badge variant="warn">{i18nT('components.notifications.notificationDetailPanel.unread')}</Badge>
        }
        {n.acked
          ? <button className="text-[13px] text-muted cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(unackNotification(n.ts))}><MailOpen className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.mark_unread')}</button>
          : <button className="text-[13px] text-ok cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(ackNotification(n.ts))}><Check className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.mark_read')}</button>
        }
      </div>

      {/* Source & navigation */}
      <div className="px-5 py-2.5 border-b border-border flex items-center gap-2 flex-wrap shrink-0">
        <span className="text-[12px] text-muted uppercase tracking-[.04em] font-medium">{i18nT('components.notifications.notificationDetailPanel.source')}</span>
        <span className="text-[13px] text-text">{km.label}{n.kind === 'cron' && n.job_id ? ` (${n.job_id.slice(0, 8)})` : n.kind === 'taskrunner' && n.task_id ? ` (${n.task_id.slice(0, 8)})` : (directSlot || relatedSlot) ? ` · ${(directSlot || relatedSlot)!.title || (directSlot || relatedSlot)!.key}` : ''}</span>
        <span className="flex-1" />
        {/* Jump-to buttons */}
        {n.kind === 'cron' && (
          <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => navigate('/schedule')}><Clock className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_cron_jobs')}</button>
        )}
        {n.kind === 'cron' && n.job_id && n.slot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(n.slot!)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.continue_session')}</button>
        )}
        {n.kind === 'cron' && n.job_id && !n.slot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { const res = await api.cronToChat(n.job_id!); if (res.error) { logError('cronToChat error', res.error); return }; if (res.slot) { dispatch(switchSlot(res.slot)); navigate('/chat') } } catch (e) { logError('cronToChat failed', e) } }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_last_result')}</button>
        )}
        {directSlot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(directSlot.key)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.go_to_chat')}</button>
        )}
        {!directSlot && n.slot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await dispatch(resumeFromHistory({ key: n.slot!, title: n.title })); navigate('/chat') } catch (e) { logError('Resume failed', e) } }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.resume_chat')}</button>
        )}
        {!directSlot && !n.slot && relatedSlot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(relatedSlot.key)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.go_to_chat')}</button>
        )}
        {safeHttpUrl(n.slack_link ?? '') && (
          <a href={safeHttpUrl(n.slack_link ?? '')!} target="_blank" rel="noopener noreferrer" className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body no-underline inline-flex items-center gap-1"><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.open_in_slack')}</a>
        )}
        {/* RFC Phase 4: dashboard-internal deep link (validated path-only). */}
        {safeInternalUrl(n.url) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => navigate(safeInternalUrl(n.url)!)}><ArrowUpRight className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.open')}</button>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="msg-content bg-card border border-border rounded-lg px-5 py-4 text-sm leading-relaxed text-text shadow-[inset_0_1px_0_var(--card-hl)] max-w-[820px] overflow-x-auto break-words">
          <MarkdownRenderer content={n.body || ''} />
        </div>

        {/* Kind-specific actions */}
        {n.kind === 'approval' && (
          <div className="flex gap-3 mt-4">
            <button className="px-4 py-2 rounded-lg bg-ok text-ok-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await api.resolveApproval(n.approval_id || n.ts, 'approve'); dispatch(deleteNotification(n.ts)); onClose() } catch (e) { logError('Approve failed', e) } }}><CheckCircle className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.approve')}</button>
            <button className="px-4 py-2 rounded-lg bg-danger text-danger-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await api.resolveApproval(n.approval_id || n.ts, 'reject'); dispatch(deleteNotification(n.ts)); onClose() } catch (e) { logError('Reject failed', e) } }}><Ban className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.reject')}</button>
          </div>
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
                <button key={a.id} className="px-4 py-2 rounded-lg border border-accent/40 bg-transparent text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle transition-all font-body" onClick={() => { navigate(safeInternalUrl(a.url)!); onClose() }}>{a.label}</button>
              ))}
            </div>
          )
        })()}
        {n.kind === 'cron' && n.job_id && (
          <CronAckBar key={n.ts} notification={n} onDone={onClose} />
        )}
        {n.kind === 'taskrunner' && n.task_id && (
          <div className="flex gap-3 mt-4">
            <button className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => {
              try { const res = await api.taskRunToChat(n.task_id!); if (res.slot) { dispatch(switchSlot(res.slot)); navigate('/chat') } } catch (e) { logError('Task nav failed', e) }
            }}><MessageSquare className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.continue_in_chat')}</button>
            <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => navigate('/projects')}><ClipboardList className="lucide-inline" /> {i18nT('components.notifications.notificationDetailPanel.view_project')}</button>
          </div>
        )}
      </div>
    </div>
  )
}
