import { memo, useEffect, useRef, useState } from 'react'
import { Pin, PinOff, Copy, Link2, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import Clickable from '../../components/Clickable'
import type { ChatPin } from '../../api/pins'

interface PinnedMessagesPanelProps {
  pins: ChatPin[]
  loading: boolean
  slotKey: string
  slotTitle?: string
  mode?: string
  onClose: () => void
  onJumpToMessage: (messageTs: string) => void
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

const PinnedMessagesPanel = memo(function PinnedMessagesPanel({
  pins, loading, slotKey, slotTitle, mode, onClose, onJumpToMessage, onUnpin,
}: PinnedMessagesPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const panel = panelRef.current
    panel?.focus()
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      onClose()
    }
    panel?.addEventListener('keydown', handleEscape)
    const interval = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => {
      panel?.removeEventListener('keydown', handleEscape)
      window.clearInterval(interval)
    }
  }, [onClose])

  return (
    <div
      ref={panelRef}
      role="region"
      tabIndex={-1}
      aria-label={i18nT('pages.chat.pins.pinned_messages')}
      className="flex flex-col h-full bg-bg"
      data-testid="pinned-messages-panel"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2 text-sm font-medium text-text">
          <Pin size={14} />
          <span>{i18nT('pages.chat.pins.pinned_messages')}</span>
          {pins.length > 0 && <span className="text-muted text-xs">({pins.length})</span>}
        </div>
        <button
          onClick={onClose}
          className="text-muted hover:text-text p-1 rounded transition-colors"
          aria-label={i18nT('pages.chat.pins.close_panel')}
        >
          <X size={14} />
        </button>
      </div>

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
            onClick={() => onJumpToMessage(pin.message_ts)}
            data-testid="pin-entry"
            aria-label={i18nT('pages.chat.pins.jump_to_message')}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-muted uppercase">
                {pin.role === 'user' ? i18nT('pages.chat.pins.you') : i18nT('pages.chat.pins.assistant')}
              </span>
              <span className="text-[11px] text-muted" title={new Date(pin.pinned_at).toLocaleString()}>
                {relativeTime(pin.pinned_at, now)}
              </span>
            </div>
            <div className="text-sm text-text line-clamp-2 leading-snug">
              {pin.preview}
            </div>
            {/* Hover actions */}
            <div className="flex items-center gap-1 mt-0.5 opacity-0 group-hover/pin:opacity-100 transition-opacity">
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
