import { useState, useCallback, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAppSelector, useAppDispatch } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import { PageHeader, StatCard, Card, CardTitle, EmptyState } from '../components/ui'
import InfoTip from '../components/InfoTip'
import NotificationFeed from '../components/notifications/NotificationFeed'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import type { Notification } from '../types'

import { i18nT } from '../i18n/t'

/** Deep-link query param naming one notification by its `ts` store id:
 *  `/notifications?note=<ts>`. External pushers (e.g. an ntfy bridge relaying
 *  the WS notification stream) hard-code this name in their Click URLs, so it
 *  is part of the page's public contract — documented in
 *  docs/system-specs/features/app-notifications.md. */
export const NOTE_DEEP_LINK_PARAM = 'note'

/**
 * Full Notifications page (route /notifications). Page chrome + master/detail
 * layout only; the feed (filter/list) and detail view are the same shared
 * components rendered by the topbar bell popover, so behavior stays identical
 * in both surfaces. This page owns the selection state and stat cards.
 */
export default function NotificationsPage() {
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  // Deep-link id captured off the URL, held here until the feed can resolve
  // it. The feed loads asynchronously (fetch on app mount, rows also arrive
  // over WS), so the id must survive past first render instead of being
  // resolved against a possibly-empty list.
  const [pendingNoteTs, setPendingNoteTs] = useState<string | null>(null)
  const isMobile = useIsMobile()

  const unread = items.filter(n => !n.acked).length
  const byCat = useCallback((k: string) => items.filter(n => n.kind === k).length, [items])
  // Derived from items so deleting/clearing the selected notification clears the
  // detail automatically (no separate selection bookkeeping needed).
  const selected = items.find(n => n.ts === selectedTs) || null

  // Auto-ack on select. Also disarms any still-pending deep link: an explicit
  // user tap outranks the URL, so a deep-link target that shows up later must
  // not yank the selection away from (or auto-ack over) the user's choice.
  const handleSelect = useCallback((n: Notification) => {
    setPendingNoteTs(null)
    setSelectedTs(n.ts)
    if (!n.acked) dispatch(ackNotification(n.ts))
  }, [dispatch])

  // Capture the deep-link param and consume it immediately (history REPLACE,
  // never push): a reload or back-navigation must not re-select and re-ack.
  // The captured id lives in component state from here on, so consuming the
  // param cannot drop a deep link the still-loading feed has yet to satisfy.
  useEffect(() => {
    const id = searchParams.get(NOTE_DEEP_LINK_PARAM)
    if (!id) return
    setPendingNoteTs(id)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete(NOTE_DEEP_LINK_PARAM)
      return next
    }, { replace: true })
  }, [searchParams, setSearchParams])

  // Deep-link id resolved to a real note: handed to the feed as revealTs so
  // the feed (which owns stacking and its own scroll container) can expand a
  // collapsed stack and bring the row into view.
  const [revealTs, setRevealTs] = useState<string | null>(null)

  // Resolve the pending deep link once the note is in the loaded feed. Routed
  // through handleSelect so ack semantics and detail behavior are identical to
  // a tapped row (mobile then shows the full-width detail via the same
  // `isMobile && selected` branch a tap uses). Stays armed while unmatched:
  // the id belonging to a slow fetch resolves on the store update that
  // delivers it, and an expired/cleared id simply never matches — the plain
  // page renders with nothing selected and no error surface.
  useEffect(() => {
    if (!pendingNoteTs) return
    const n = items.find(i => i.ts === pendingNoteTs)
    if (!n) return
    handleSelect(n)
    setRevealTs(n.ts)
  }, [pendingNoteTs, items, handleSelect])

  return (
    <>
      <PageHeader title={i18nT('pages.notificationsPage.notifications')} subtitle={i18nT('pages.notificationsPage.all_agent_activity_cron_results_webhooks_and_app')} />
      <div className="px-6 pb-8 flex-1 min-h-0 flex flex-col overflow-hidden">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))] mb-4 shrink-0">
          <StatCard label={i18nT('pages.notificationsPage.total')} value={items.length} accent />
          <StatCard label={i18nT('pages.notificationsPage.unread')} value={unread} />
          <StatCard label={i18nT('pages.notificationsPage.cron')} value={byCat('cron')} />
          <StatCard label={i18nT('pages.notificationsPage.hooks')} value={byCat('hook')} />
          <StatCard label={i18nT('pages.notificationsPage.heartbeat')} value={byCat('heartbeat')} />
        </div>

        {/* Split layout: feed + detail */}
        <div className="flex-1 min-h-0 flex gap-4">
          {/* Left: feed */}
          <div className={`flex flex-col shrink-0 ${isMobile ? 'w-full' : 'min-w-[320px] max-w-[420px] w-[40%]'} ${isMobile && selected ? 'hidden' : ''}`}>
            <Card className="flex flex-col flex-1 min-h-0">
              <CardTitle>{i18nT('pages.notificationsPage.activity_feed')} <InfoTip text={i18nT('pages.notificationsPage.click_a_notification_to_view_details_jump_to_the')} /></CardTitle>
              <NotificationFeed selectedTs={selectedTs} onSelect={handleSelect} revealTs={revealTs} />
            </Card>
          </div>

          {/* Right: detail panel */}
          {isMobile && selected ? (
            <div className="flex-1 min-w-0">
              <Card className="flex flex-col h-full min-h-0">
                <button className="flex items-center gap-1 px-2 py-1.5 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none mb-1" onClick={() => setSelectedTs(null)}>
                  <ArrowLeft size={14} /> {i18nT('pages.notificationsPage.back')}
                </button>
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            </div>
          ) : !isMobile && <div className="flex-1 min-w-0">
            {selected ? (
              <Card className="flex flex-col h-full min-h-0">
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            ) : (
              <Card className="flex items-center justify-center h-full">
                <EmptyState icon={<ArrowLeft className="lucide-inline" />} title={i18nT('pages.notificationsPage.select_a_notification')} subtitle={i18nT('pages.notificationsPage.click_any_item_to_view_details_and_navigate_to_i')} />
              </Card>
            )}
          </div>}
        </div>
      </div>
    </>
  )
}
