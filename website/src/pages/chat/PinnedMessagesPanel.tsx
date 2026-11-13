import { memo, useEffect, useRef, useState, type ReactNode } from 'react'
import { PinOff, Copy, Link2, Check, X, CornerDownLeft } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import { HOVER_NONE_ACTIONS_ROW_CLS, HOVER_NONE_ACTION_BTN_CLS } from '../../utils/touchActions'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import type { ChatPin } from '../../api/pins'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

interface PinnedMessagesPanelProps {
  pins: ChatPin[]
  loading: boolean
  slotKey: string
  slotTitle?: string
  mode?: string
  onJumpToMessage: (messageTs: string, mid?: string) => void
  /** Put a pinned USER message back in the composer (never sent). Absent: no such button. */
  onPutInComposer?: (messageTs: string, mid?: string) => void
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

/** Longest preview text, in characters, that a pin's accessible name carries. */
const PIN_NAME_LABEL_MAX = 60

/**
 * Accessible name for one pin entry: who wrote it plus the start of its text.
 *
 * Every entry is the same jump action, so a name without the message would
 * read "Jump to message" once per pin and a screen reader could not tell them
 * apart. Whitespace is collapsed and the text cut at a word near
 * `PIN_NAME_LABEL_MAX`, so the name stays short. A pin with no preview text
 * keeps the plain action name.
 */
function pinEntryName(pin: ChatPin): string {
  const text = (pin.preview ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return i18nT('pages.chat.pins.jump_to_message')
  let label = text
  // Counted in code points so the cut never splits an emoji's surrogate pair.
  const chars = Array.from(text)
  if (chars.length > PIN_NAME_LABEL_MAX) {
    const cut = chars.slice(0, PIN_NAME_LABEL_MAX).join('')
    const space = cut.lastIndexOf(' ')
    label = `${(space > PIN_NAME_LABEL_MAX / 2 ? cut.slice(0, space) : cut).trimEnd()}…`
  }
  const author = pin.role === 'user' ? i18nT('pages.chat.pins.you') : i18nT('pages.chat.pins.assistant')
  return i18nT('pages.chat.pins.jump_to_named_message', { author, label })
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
  pins, loading, slotKey, slotTitle, mode, onJumpToMessage, onPutInComposer, onUnpin,
}: PinnedMessagesPanelProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [now, setNow] = useState(() => Date.now())

  // Which pin's Copy / Copy-link button is currently showing its outcome.
  // Keyed by pin id (not a bool) so only the row the user clicked flips its
  // icon, matching the in-chat message action buttons which give a 1.5s
  // Check-icon confirmation. Without this the panel's Copy/Link buttons ran
  // their side effect silently and looked inert. `ok: false` is the refused
  // clipboard write (permission, insecure context): it flips the icon to a
  // failed state instead of leaving it unchanged, so "did it copy?" has an
  // answer either way.
  const [copyFeedback, setCopyFeedback] = useState<{ id: string; ok: boolean } | null>(null)
  const [linkFeedback, setLinkFeedback] = useState<{ id: string; ok: boolean } | null>(null)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const linkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // A success is spoken by the polite live region below; the button title and
  // icon show it to sighted users only. `nonce % 2` zero-width spaces make the
  // text differ when the SAME result repeats (Copy, then Copy link), because a
  // live region speaks only on a text change. Same pattern as the composer's
  // attachment announcer.
  const [announcement, setAnnouncement] = useState<{ text: string; nonce: number } | null>(null)
  // A refused write renders through ErrorNotice, with the recovery the user
  // has; its role="alert" is what a screen reader hears for a failure. The
  // notice is keyed by `nonce`, so a repeat of the same refusal mounts a fresh
  // alert and is spoken again rather than leaving the old text in place.
  const [copyError, setCopyError] = useState<{ text: string; nonce: number } | null>(null)

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

  const report = (ok: boolean, failedMessage: string) => {
    if (ok) {
      setCopyError(null)
      setAnnouncement(prev => ({ text: i18nT('pages.chat.pins.copied'), nonce: (prev?.nonce ?? 0) + 1 }))
    } else {
      setAnnouncement(null)
      setCopyError(prev => ({ text: failedMessage, nonce: (prev?.nonce ?? 0) + 1 }))
    }
  }
  const flashCopied = (id: string, ok: boolean) => {
    setCopyFeedback({ id, ok })
    report(ok, i18nT('pages.chat.pins.copy_text_failed'))
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    copyTimerRef.current = setTimeout(() => { copyTimerRef.current = null; setCopyFeedback(null) }, 1500)
  }
  const flashLinkCopied = (id: string, ok: boolean) => {
    setLinkFeedback({ id, ok })
    report(ok, i18nT('pages.chat.pins.copy_link_failed'))
    if (linkTimerRef.current) clearTimeout(linkTimerRef.current)
    linkTimerRef.current = setTimeout(() => { linkTimerRef.current = null; setLinkFeedback(null) }, 1500)
  }
  /** Outcome glyph for a copy button once its promise settled. */
  const outcomeIcon = (fb: { id: string; ok: boolean } | null, id: string, idle: ReactNode) => {
    if (fb?.id !== id) return idle
    return fb.ok ? <Check size={12} className="text-ok" /> : <X size={12} className="text-danger" />
  }
  const copyLabel = (fb: { id: string; ok: boolean } | null, id: string, idle: string) => {
    if (fb?.id !== id) return idle
    return fb.ok ? i18nT('pages.chat.pins.copied') : i18nT('pages.chat.pins.copy_failed')
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
            aria-label={pinEntryName(pin)}
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
            {/* Its own row, outside the hover row below, so that row does not grow. */}
            {pin.role === 'user' && onPutInComposer && (
              <div className="flex">
                <Btn
                  type="button"
                  onClick={(e) => { e.stopPropagation(); onPutInComposer(pin.message_ts, pin.mid) }}
                  className={HOVER_NONE_ACTION_BTN_CLS}
                >
                  <CornerDownLeft size={14} aria-hidden="true" />
                  {i18nT('pages.chat.pins.put_in_composer')}
                </Btn>
              </div>
            )}
            {/* Hover actions — forced visible + 40px targets where the pointer cannot hover */}
            <div data-testid="pin-actions" className={`flex items-center gap-1 mt-0.5 opacity-0 group-hover/pin:opacity-100 focus-within:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
              <button
                onClick={(e) => { e.stopPropagation(); copyToClipboard(pin.preview).then((ok) => flashCopied(pin.id, ok), () => flashCopied(pin.id, false)) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={copyLabel(copyFeedback, pin.id, i18nT('pages.chat.pins.copy_preview'))}
                aria-label={i18nT('pages.chat.pins.copy_preview')}
              >
                {outcomeIcon(copyFeedback, pin.id, <Copy size={12} />)}
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); copySessionLink(slotKey, slotTitle, pin.message_ts, mode, pin.mid).then((ok) => flashLinkCopied(pin.id, ok), () => flashLinkCopied(pin.id, false)) }}
                className="text-muted hover:text-text p-0.5 rounded transition-colors"
                title={copyLabel(linkFeedback, pin.id, i18nT('pages.chat.pins.copy_link'))}
                aria-label={i18nT('pages.chat.pins.copy_link')}
              >
                {outcomeIcon(linkFeedback, pin.id, <Link2 size={12} />)}
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
      {/* No hand-off: this panel sits beside the chat composer, whose unsent
          draft the hand-off would navigate away from. */}
      <ErrorNotice
        key={copyError?.nonce ?? 0}
        message={copyError?.text ?? null}
        onDismiss={() => setCopyError(null)}
        className="mx-3 mb-2"
        testId="pins-copy-error"
      />
      {/* Stays mounted so the region exists before its text arrives. */}
      <span className="sr-only" aria-live="polite" data-testid="pins-copy-status">
        {announcement ? `${announcement.text}${'\u200B'.repeat(announcement.nonce % 2)}` : ''}
      </span>
    </div>
  )
})

export { PinnedMessagesPanel }
export type { PinnedMessagesPanelProps }
