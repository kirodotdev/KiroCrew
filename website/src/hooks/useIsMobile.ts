import { useSyncExternalStore } from 'react'

const MOBILE_BREAKPOINT = 768
const MOBILE_QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`

const mql = typeof window !== 'undefined' ? window.matchMedia(MOBILE_QUERY) : null

function subscribe(cb: () => void) {
  mql?.addEventListener('change', cb)
  return () => mql?.removeEventListener('change', cb)
}

function getSnapshot() {
  return mql?.matches ?? false
}

function getServerSnapshot() {
  return false
}

export function useIsMobile() {
  const match = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
  // In embed mode (IntelliJ plugin minimal view), never report as mobile
  // regardless of viewport width. The plugin panel can be narrow but should
  // always behave as desktop (Enter to send, full icon row, no collapsed UI).
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/embed/')) return false
  return match
}
