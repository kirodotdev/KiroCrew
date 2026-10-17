import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppSelector } from '../store'

/**
 * Bridge that lets the IntelliJ plugin trigger react-router navigation
 * via a CustomEvent, instead of doing a full page reload via
 * `window.location.replace()`.
 *
 * Why: switching tabs in the plugin should feel like switching tabs in
 * a browser — the React tree stays mounted, Redux state survives, and
 * background streaming on other slots keeps running. A full reload
 * would tear all that down.
 *
 * Plugin side dispatches:
 *   window.dispatchEvent(new CustomEvent('kirocrew-soft-navigate', {
 *     detail: { url: '/embed/chat/chat-7?sid=chat-7' }
 *   }))
 *
 * This component listens for that event and invokes react-router's
 * `navigate()` with the path portion of the URL.
 *
 * Also reports slot titles to the plugin via `window.__kirocrewReportSlotTitle`
 * (a function the plugin installs once JCEF loads). The plugin caches
 * these to display human-readable tab labels instead of slugs.
 *
 * Mount once in App.tsx inside the embed-mode branch.
 */
export default function KiroCrewNavBridge() {
  const navigate = useNavigate()
  const slots = useAppSelector(s => s.dashboard.slots)

  // Soft navigation listener
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      const url = typeof detail?.url === 'string' ? detail.url : null
      if (!url) return
      try {
        const u = new URL(url, window.location.origin)
        navigate(u.pathname + u.search + u.hash)
      } catch {
        navigate(url)
      }
    }
    window.addEventListener('kirocrew-soft-navigate', handler)
    return () => window.removeEventListener('kirocrew-soft-navigate', handler)
  }, [navigate])

  // Report slot titles to the plugin whenever slots change
  useEffect(() => {
    const fn = (window as unknown as { __kirocrewReportSlotTitle?: (slug: string, title: string) => void }).__kirocrewReportSlotTitle
    if (typeof fn !== 'function') return
    for (const slot of slots) {
      if (slot.key && slot.title) {
        try { fn(slot.key, slot.title) } catch { /* swallow — bridge may be tearing down */ }
      }
    }
  }, [slots])

  return null
}
