import { useEffect, useState } from 'react'
import { ExternalLink, X } from 'lucide-react'
import Glass from '../Glass'
import { IconButton } from '../ui'
import { i18nT } from '../../i18n/t'
import { safeHttpUrl } from '../../lib/safeUrl'
import { BANNER_AUTO_HIDE_MS } from '../../hooks/notificationBanner'

type ResultNotice = { source: 'file-menu'; message: string; url?: string }

/**
 * A local action result on the host toast bus, not a persisted feed notification.
 *
 * Rendered in flow inside `NotificationBanner`'s fixed region (its `after` slot), so
 * it stacks under any live feed card instead of covering it. The link shows only the
 * destination's hostname: a long signed URL would otherwise wrap into lines of
 * characters that tell the reader nothing; the full URL stays on the `title`.
 */
export default function FileMenuResultToast() {
  const [notice, setNotice] = useState<ResultNotice | null>(null)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const held = hovered || focused
  useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (detail?.source !== 'file-menu' || typeof detail.message !== 'string' || !detail.message.trim()) return
      setNotice({ source: 'file-menu', message: detail.message,
        url: typeof detail.url === 'string' ? safeHttpUrl(detail.url) ?? undefined : undefined })
    }
    window.addEventListener('mc:notify', receive)
    return () => window.removeEventListener('mc:notify', receive)
  }, [])
  useEffect(() => {
    if (!notice || held) return
    const timer = window.setTimeout(() => setNotice(null), BANNER_AUTO_HIDE_MS)
    return () => window.clearTimeout(timer)
  }, [notice, held])
  if (!notice) return null
  return (
    <Glass variant="panel" radius={16} role="status" aria-live="polite"
      data-testid="file-menu-result-toast"
      className="notif-material glass-shadow pointer-events-auto mt-2 block rounded-2xl px-3 py-2.5 text-[13px]"
      onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false) }}>
      <div className="flex items-start gap-2">
        <span className="flex-1 min-w-0 break-words text-text">{notice.message}</span>
        <IconButton aria-label={i18nT('components.notifications.notificationBanner.dismiss_notification')} onClick={() => { setNotice(null); setHovered(false); setFocused(false) }}>
          <X size={14} />
        </IconButton>
      </div>
      {notice.url && <a href={notice.url} title={notice.url} target="_blank" rel="noopener noreferrer"
        className="mt-1 inline-flex max-w-full items-center gap-1 text-accent hover:underline">
        <span className="truncate">{new URL(notice.url).hostname}</span>
        <ExternalLink size={12} aria-hidden="true" className="shrink-0" />
      </a>}
    </Glass>
  )
}

/**
 * The toast for the popout and embed frames. They render the file-menu rows (the
 * chat side panels' markdown header) but have no `NotificationBanner` region to
 * stack into, so the toast gets its own fixed corner with the same width rules.
 */
export function FileMenuResultToastFrame() {
  return (
    <div data-testid="file-menu-result-toast-frame"
      className="fixed z-[59] pointer-events-none top-safe-offset-1 right-safe-offset-3 left-safe-offset-3 sm:left-auto sm:w-[340px]">
      <FileMenuResultToast />
    </div>
  )
}
