import { memo, useEffect, useState } from 'react'
import { PinOff, Copy, Link2 } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../../utils/touchActions'
import Clickable from '../../components/Clickable'
import type { ChatPin } from '../../api/pins'

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
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(interval)
  }, [])

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
            className="group/pin flex flex-col gap-1 px-3 py-2.5 rounded-md hover:bg-hover cursor-pointer transition-colors mb-1"
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
            <div className={`flex items-center gap-1 mt-0.5 opacity-0 group-hover/pin:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
              <button
                onClick={(e) => { e.stopPropagation(); copyToClipboard(pin.preview) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={i18nT('pages.chat.pins.copy_preview')}
                aria-label={i18nT('pages.chat.pins.copy_preview')}
              >
                <Copy size={12} />
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); copySessionLink(slotKey, slotTitle, pin.message_ts, mode) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={i18nT('pages.chat.pins.copy_link')}
                aria-label={i18nT('pages.chat.pins.copy_link')}
              >
                <Link2 size={12} />
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
