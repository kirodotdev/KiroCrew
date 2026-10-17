import { useEffect, useRef, useCallback, useState } from 'react'

export type WatchStatus = 'idle' | 'connecting' | 'open' | 'error'

/** What a frame says besides its text. `initial` marks the stream's first
 *  frame: the backend's connect-time read, which reports the file as it is
 *  rather than a change to it. Every later frame follows a write. */
export interface FileWatchFrame { initial: boolean }

/** Subscribe to SSE file-change events at GET /api/file-watch?path=... */
export function useFileWatch(
  filePath: string | null,
  onContent: (content: string, frame: FileWatchFrame) => void,
) {
  const cbRef = useRef(onContent)
  cbRef.current = onContent
  const esRef = useRef<EventSource | null>(null)
  const [status, setStatus] = useState<WatchStatus>('idle')

  const stop = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setStatus('idle')
  }, [])

  useEffect(() => {
    if (!filePath) { setStatus('idle'); return }

    setStatus('connecting')
    const es = new EventSource('/api/file-watch?path=' + encodeURIComponent(filePath))
    esRef.current = es

    es.onopen = () => setStatus('open')
    let initial = true
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data)
        if (data.content != null) {
          const frame = { initial }
          initial = false
          cbRef.current(data.content, frame)
        }
      } catch { /* ignore parse errors */ }
    }
    es.onerror = () => {
      // A directory or missing path makes the backend return 404 (see
      // api_file_watch's os.path.isfile guard); a refused/closed stream also
      // fires onerror. EventSource AUTO-RECONNECTS on error, so a permanently
      // bad path (e.g. a directory opened via a clickable path chip) turns into
      // an endless reconnect loop hammering the endpoint. Close it explicitly
      // and settle into a stable 'error' state instead of spinning. A genuine
      // transient drop simply re-subscribes when filePath changes and the
      // effect re-runs (the panel can also re-open the file to retry).
      es.close()
      esRef.current = null
      setStatus('error')
    }

    return () => {
      es.close()
      esRef.current = null
      setStatus('idle')
    }
  }, [filePath])

  return { stop, status }
}
