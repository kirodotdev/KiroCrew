import { useCallback, useRef, useState } from 'react'
import { api } from '../api/client'
import { isDeadlineError } from '../api/queryClient'

export type BrowseListError = false | 'failed' | 'timeout'

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>

/**
 * The one subtle rule both directory pickers need: a superseded drill's rejection must not
 * raise the error on the listing that replaced it. Held here rather than spelled twice, so
 * the guard and the failed/timeout split cannot drift apart between the two callers.
 *
 * `nav` is whatever the caller needs handed back to its own success path, which is where
 * the two pickers genuinely differ (one writes a trailing separator and refocuses).
 */
export function useBrowseDirs<N = void>(onData: (d: BrowseDirsResult, nav: N) => void) {
  const [listError, setListError] = useState<BrowseListError>(false)
  const seqRef = useRef(0)
  const onDataRef = useRef(onData)
  onDataRef.current = onData

  const browse = useCallback((path?: string, nav?: N) => {
    const seq = ++seqRef.current
    setListError(false)
    api.browseDirs(path).then(d => {
      onDataRef.current(d, nav as N)
    }).catch((e: unknown) => {
      if (seq === seqRef.current) setListError(isDeadlineError(e) ? 'timeout' : 'failed')
    })
  }, [])

  return { listError, browse }
}
