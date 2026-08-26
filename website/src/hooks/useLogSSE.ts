import { useEffect, useRef, useCallback } from 'react'

export function useLogSSE(onMessage: (data: { level: string; msg: string }) => void) {
  const ref = useRef<EventSource | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cb = useRef(onMessage)
  cb.current = onMessage

  const start = useCallback(() => {
    if (ref.current) return
    const sse = new EventSource('/api/logs')
    ref.current = sse
    sse.onmessage = (e) => {
      try { cb.current(JSON.parse(e.data)) } catch { /* ignore */ }
    }
    sse.onerror = () => {
      sse.close()
      ref.current = null
      // Store the reconnect handle so stop()/unmount can cancel it. Without
      // this, a reconnect scheduled during the 3s error window fires after
      // unmount, opens a new EventSource on the orphaned ref, and leaks — with
      // an unbounded reconnect loop that no one can close.
      timer.current = setTimeout(start, 3000)
    }
  }, [])

  const stop = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    ref.current?.close()
    ref.current = null
  }, [])

  useEffect(() => {
    start()
    return stop
  }, [start, stop])
}
