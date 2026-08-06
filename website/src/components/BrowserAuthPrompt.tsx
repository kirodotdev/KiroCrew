import { useState, useEffect, useCallback } from 'react'
import { Lock, X } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
interface AuthEvent {
  gate_type?: string
  url?: string
  hint?: string
}

/**
 * BrowserAuthPrompt — amber notification banner shown when the browser
 * encounters an authentication gate it cannot automatically pass.
 * Provides a Retry button (POSTs to /api/browser-auth-retry) and Dismiss.
 * Auto-dismisses when the next `page_loaded` event arrives.
 */
export default function BrowserAuthPrompt() {
  const [visible, setVisible] = useState(false)
  const [authEvent, setAuthEvent] = useState<AuthEvent | null>(null)

  const retryMutation = useMutation({ mutationFn: () => api.browserAuthRetry() })

  const handleEvent = useCallback((e: Event) => {
    const detail = (e as CustomEvent).detail
    if (!detail) return
    if (detail.event === 'auth_required') {
      setAuthEvent({
        gate_type: detail.gate_type,
        url: detail.url,
        hint: detail.hint,
      })
      setVisible(true)
    } else if (detail.event === 'page_loaded' || detail.event === 'auth_retry') {
      // Auto-dismiss on successful navigation or retry
      setVisible(false)
    }
  }, [])

  useEffect(() => {
    window.addEventListener('kirocrew-browser-event', handleEvent)
    return () => window.removeEventListener('kirocrew-browser-event', handleEvent)
  }, [handleEvent])

  const handleRetry = useCallback(() => {
    // If successful, the backend will broadcast page_loaded or auth_retry, auto-dismissing
    retryMutation.mutate()
  }, [retryMutation])

  if (!visible || !authEvent) return null

  return (
    <div
      className="fixed top-14 left-0 right-0 z-50 flex items-center gap-3 px-4 py-3 border-b border-border shadow-sm"
      style={{ backgroundColor: 'var(--warn)', color: 'var(--text)' }}
    >
      <Lock size={16} className="shrink-0" style={{ color: 'var(--text)' }} />
      <div className="flex flex-col gap-0.5 flex-1 min-w-0">
        <span className="text-[13px] font-medium">{i18nT('components.browserAuthPrompt.browser_needs_authentication')}</span>
        <span className="text-[12px] opacity-80 truncate">
          {authEvent.hint || i18nT('components.browserAuthPrompt.auth_gate', { gateType: authEvent.gate_type || i18nT('components.browserAuthPrompt.unknown') })}
        </span>
      </div>
      <button
        onClick={handleRetry}
        disabled={retryMutation.isPending}
        className="px-3 py-1 text-[13px] font-medium rounded border border-border bg-card hover:bg-bg-hover disabled:opacity-50 transition-colors"
        style={{ color: 'var(--text)' }}
      >
        {retryMutation.isPending ? i18nT('components.browserAuthPrompt.retrying') : i18nT('components.browserAuthPrompt.retry')}
      </button>
      <button
        onClick={() => setVisible(false)}
        className="p-1 rounded hover:bg-bg-hover transition-colors"
        aria-label={i18nT('components.browserAuthPrompt.dismiss_auth_prompt')}
        style={{ color: 'var(--text)' }}
      >
        <X size={16} />
      </button>
    </div>
  )
}
