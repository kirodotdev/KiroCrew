import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import Glass from '../Glass'
import { IconButton } from '../ui'
import { i18nT } from '../../i18n/t'
import { safeHttpUrl } from '../../lib/safeUrl'
import { BANNER_AUTO_HIDE_MS } from '../../hooks/notificationBanner'

type ResultNotice = { source: 'file-menu'; message: string; url?: string }

/** A local action result on the host toast bus, not a persisted feed notification. */
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
      className="notif-material glass-shadow fixed z-[59] top-safe-offset-[50px] right-safe-offset-3 left-safe-offset-3 sm:left-auto sm:w-[340px] rounded-2xl px-3 py-2.5 text-[13px]"
      onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false) }}>
      <div className="flex items-start gap-2">
        <span className="flex-1 min-w-0 break-words text-text">{notice.message}</span>
        <IconButton aria-label={i18nT('components.notifications.notificationBanner.dismiss_notification')} onClick={() => { setNotice(null); setHovered(false); setFocused(false) }}>
          <X size={14} />
        </IconButton>
      </div>
      {notice.url && <a href={notice.url} target="_blank" rel="noopener noreferrer"
        className="block mt-1 break-all text-accent hover:underline">{notice.url}</a>}
    </Glass>
  )
}
