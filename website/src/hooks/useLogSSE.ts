import { useEffect, useRef, useCallback } from 'react'

export function useLogSSE(onMessage: (data: { level: string; msg: string }) => void) {
  const ref = useRef<EventSource | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const disposedRef = useRef(false)
  const cb = useRef(onMessage)
  cb.current = onMessage

  const start = useCallback(() => {
    if (disposedRef.current) return
    if (ref.current) return
    const sse = new EventSource('/api/logs')
    ref.current = sse
    sse.onmessage = (e) => {
      if (disposedRef.current) return
      try { cb.current(JSON.parse(e.data)) } catch { /* ignore */ }
    }
    sse.onerror = () => {
      sse.close()
      ref.current = null
      if (disposedRef.current) return
      timerRef.current = setTimeout(start, 3000)
    }
  }, [])

  const stop = useCallback(() => {
    disposedRef.current = true
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    ref.current?.close()
    ref.current = null
  }, [])

  useEffect(() => {
    disposedRef.current = false
    start()
    return stop
  }, [start, stop])
}
