import { memo, useEffect, useRef, useState } from 'react'
import { PinOff, Copy, Link2, Check } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../../utils/touchActions'
import Clickable from '../../components/Clickable'
import type { ChatPin } from '../../api/pins'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

interface PinnedMessagesPanelProps {
  pins: ChatPin[]
  loading: boolean
  slotKey: string
  slotTitle?: string
  mode?: string
  onJumpToMessage: (messageTs: string, mid?: string) => void
  onUnpin: (id: string) => void
}

function relativeTime(iso: string, now: number): string {
  const diff = now - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return i18nT('pages.chat.pins.just_now')
  if (mins < 60) return i18nT('pages.chat.pins.minutes_ago', { count: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return i18nT('pages.chat.pins.hours_ago', { count: hrs })
  const days = Math.floor(hrs / 24)
  return i18nT('pages.chat.pins.days_ago', { count: days })
}

/**
 * Body of the side panel's Pins tab.
 *
 * Deliberately chrome-less: no title row and no close button. The panel's tab
 * strip already names this view and owns closing it, so a header here would be
 * a second title and a second close affordance for one surface.
 *
 * No focus-on-mount either, which is what the standalone panel this replaced
 * used to do to reach its own Escape handler. ActivityViewer's Escape handler is
 * bound to its container, so it fires once focus is inside the panel and not
 * while focus is still on the tab-strip control that opened it — the same for
 * every view in this panel, none of which grabs focus. Taking focus here would
 * make Pins the only one that does, against the menu's return-focus contract.
 */
const PinnedMessagesPanel = memo(function PinnedMessagesPanel({
  pins, loading, slotKey, slotTitle, mode, onJumpToMessage, onUnpin,
}: PinnedMessagesPanelProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [now, setNow] = useState(() => Date.now())

  // Which pin's Copy / Copy-link button is currently showing its "done"
  // checkmark. Keyed by pin id (not a bool) so only the row the user clicked
  // flips its icon, matching the in-chat message action buttons which give a
  // 1.5s Check-icon confirmation. Without this the panel's Copy/Link buttons
  // ran their side effect silently and looked inert.
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [linkCopiedId, setLinkCopiedId] = useState<string | null>(null)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const linkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(interval)
  }, [])

  // Clear any pending feedback-reset timers on unmount so a late setState
  // doesn't fire against a torn-down component.
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    if (linkTimerRef.current) clearTimeout(linkTimerRef.current)
  }, [])

  const flashCopied = (id: string) => {
    setCopiedId(id)
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    copyTimerRef.current = setTimeout(() => { copyTimerRef.current = null; setCopiedId(null) }, 1500)
  }
  const flashLinkCopied = (id: string) => {
    setLinkCopiedId(id)
    if (linkTimerRef.current) clearTimeout(linkTimerRef.current)
    linkTimerRef.current = setTimeout(() => { linkTimerRef.current = null; setLinkCopiedId(null) }, 1500)
  }

  return (
    <div
      role="region"
      aria-label={i18nT('pages.chat.pins.pinned_messages')}
      className="flex flex-col h-full bg-bg"
      data-testid="pinned-messages-panel"
    >
      {/* Body */}
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {loading && <div className="text-muted text-sm text-center py-4">{i18nT('pages.chat.pins.loading')}</div>}
        {!loading && pins.length === 0 && (
          <div className="text-muted text-sm text-center py-8" data-testid="pins-empty-state">
            {i18nT('pages.chat.pins.no_pinned_messages')}
          </div>
        )}
        {!loading && pins.map(pin => (
          <Clickable
            key={pin.id}
            className="group/pin flex flex-col gap-1 px-3 py-2.5 rounded-md hover:bg-bg-hover cursor-pointer transition-colors mb-1"
            onClick={() => onJumpToMessage(pin.message_ts, pin.mid)}
            data-testid="pin-entry"
            aria-label={i18nT('pages.chat.pins.jump_to_message')}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-muted uppercase">
                {pin.role === 'user' ? i18nT('pages.chat.pins.you') : i18nT('pages.chat.pins.assistant')}
              </span>
              <span className="text-[11px] text-muted" title={fmtDateTime(pin.pinned_at)}>
                {relativeTime(pin.pinned_at, now)}
              </span>
            </div>
            <div className="text-sm text-text line-clamp-2 leading-snug">
              {pin.preview}
            </div>
            {/* Hover actions — forced visible + 40px targets where the pointer cannot hover */}
            <div data-testid="pin-actions" className={`flex items-center gap-1 mt-0.5 opacity-0 group-hover/pin:opacity-100 focus-within:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
              <button
                onClick={(e) => { e.stopPropagation(); copyToClipboard(pin.preview).then((ok) => { if (ok) flashCopied(pin.id) }).catch(() => {}) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={copiedId === pin.id ? i18nT('pages.chat.pins.copied') : i18nT('pages.chat.pins.copy_preview')}
                aria-label={i18nT('pages.chat.pins.copy_preview')}
              >
                {copiedId === pin.id ? <Check size={12} className="text-ok" /> : <Copy size={12} />}
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); copySessionLink(slotKey, slotTitle, pin.message_ts, mode, pin.mid).then((ok) => { if (ok) flashLinkCopied(pin.id) }).catch(() => {}) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={linkCopiedId === pin.id ? i18nT('pages.chat.pins.copied') : i18nT('pages.chat.pins.copy_link')}
                aria-label={i18nT('pages.chat.pins.copy_link')}
              >
                {linkCopiedId === pin.id ? <Check size={12} className="text-ok" /> : <Link2 size={12} />}
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); onUnpin(pin.id) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={i18nT('pages.chat.pins.unpin')}
                aria-label={i18nT('pages.chat.pins.unpin')}
              >
                <PinOff size={12} />
              </button>
            </div>
          </Clickable>
        ))}
      </div>
    </div>
  )
})

export { PinnedMessagesPanel }
export type { PinnedMessagesPanelProps }
