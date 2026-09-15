import { useCallback, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useFileWatch } from './useFileWatch'
import { isArtifactEditing } from '../utils/artifactEditGuard'

/**
 * Debounce applied to the change signal before refetching.
 *
 * A tool that rewrites a file rarely does it in one write (truncate + append,
 * or a formatter running straight after a generator), and the watch endpoint
 * polls mtime every second — so a single logical edit can land as a short burst
 * of frames. Coalescing them into one refetch keeps a multi-MB artifact from
 * being fetched several times for one edit.
 */
const RELOAD_DEBOUNCE_MS = 400

/**
 * Live-reload a file-backed artifact when something rewrites its backing file.
 *
 * `ArtifactStore.get` reads a file-backed artifact's content straight off
 * `source_path` on every GET, so the server is always current — but the client
 * fetches once per mount and never revalidates (the shared QueryClient sets
 * `staleTime: Infinity`, so freshness is push-driven). The only push that
 * exists is the `artifact_update` WS event, and that fires from the artifact
 * mutation funnel: an agent (or any other tool) writing the file directly never
 * passes through a handler, so an open panel or detail page kept rendering
 * pre-edit content until it was closed and reopened.
 *
 * This subscribes to the existing `/api/file-watch` SSE for `sourcePath` as a
 * **change signal only** — the frame's `content` is deliberately discarded and
 * the artifact is refetched through its normal API instead. That keeps one
 * serving path for artifact content (redaction, the live/snapshot fallback and
 * the `live_dirty` recompute all live there) and sidesteps the 512KB cap on what
 * the watch stream can DELIVER, which baked HTML artifacts routinely exceed. The
 * separate cap on what it can DETECT still applies — see the known limits below.
 *
 * Two deliberate non-behaviors:
 *
 *  * **Never refetch while an edit buffer is open** (`isArtifactEditing`).
 *    Refetching moves the editor's baseline while the buffer keeps the older
 *    text, so the next Save would silently overwrite whatever just arrived —
 *    the same data-loss path `useWebSocket`'s `artifact_update` handler
 *    withholds this exact invalidation for. Nothing else is refreshed on that
 *    branch either: an external write emits no artifact event and no comment,
 *    so there is nothing else to pull. `ArtifactDetailPage` already refetches
 *    when editing ends, which is where a withheld update is picked up.
 *  * **No retry, no reconnect of our own.** `useFileWatch` closes the stream
 *    and settles into `'error'` on failure (404 for a missing/moved file, or a
 *    dropped connection), which leaves the hook inert and the surface back on
 *    today's manual-reload behavior. Passing a null `slug`/`sourcePath` — i.e.
 *    every artifact that is not file-backed — opens no stream at all.
 *
 * Two known limits, both inherited from the endpoint rather than introduced here:
 *
 *  * `api_file_watch` only emits when the redacted **first 512,000 bytes**
 *    change, so a rewrite touching only bytes past that cap produces no signal
 *    and no reload.
 *  * One EventSource per mounted surface, and each artifact popout is its own
 *    window with its own JS context. The gateway is HTTP/1.1, so popouts plus
 *    `MarkdownPanel` plus `/api/logs` share the browser's ~6-connections-per
 *    -origin budget — which is why nothing is watched unless the artifact is
 *    actually file-backed.
 */
export function useArtifactLiveReload(
  slug: string | null | undefined,
  sourcePath: string | null | undefined,
): void {
  const queryClient = useQueryClient()
  const timerRef = useRef<ReturnType<typeof setTimeout>>()

  const onChange = useCallback(() => {
    if (!slug) return
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => {
      // Re-checked HERE, not when the frame arrived: the editor may have been
      // opened during the debounce window, and the guard has to reflect the
      // state at the moment the refetch would actually land.
      if (isArtifactEditing(slug)) return
      // invalidateQueries (not setQueryData) so every mounted observer of this
      // slug repaints together, and because it refetches active queries
      // regardless of staleTime — marking the query stale under
      // `staleTime: Infinity` would repaint nothing.
      queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
    }, RELOAD_DEBOUNCE_MS)
  }, [queryClient, slug])

  // Cancel a pending refetch on unmount so it cannot fire into a torn-down tree.
  useEffect(() => () => clearTimeout(timerRef.current), [])

  useFileWatch(slug && sourcePath ? sourcePath : null, onChange)
}
