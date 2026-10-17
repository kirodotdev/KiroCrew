import { useCallback, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useVisibleTabCatchUp, settleCatchUp, type CatchUpLatch } from './useVisibleTabCatchUp'
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
 *    dropped connection), which leaves the hook inert until the stream is
 *    re-armed -- in the side panel, when a hidden tab is shown again -- and the
 *    surface back on today's manual-reload behavior. Passing a null
 *    `slug`/`sourcePath` — i.e. every artifact that is not file-backed — opens
 *    no stream at all.
 *
 * Two known limits, both inherited from the endpoint rather than introduced here:
 *
 *  * `api_file_watch` reads only the redacted **first 512,000 characters**. For
 *    a file within that cap it emits only when they change; for a file that
 *    fills it, every write emits a body-less frame, so a rewrite past the cap
 *    still reloads (debounced as above).
 *  * One EventSource per visible surface, and each artifact popout is its own
 *    window with its own JS context. The gateway is HTTP/1.1, so popouts plus
 *    `MarkdownPanel` share the browser's ~6-connections-per
 *    -origin budget — which is why nothing is watched unless the artifact is
 *    actually file-backed and on screen.
 *
 * `active` is the side panel's visibility term. `SidePanel` keeps every tab
 * mounted, so a hidden artifact tab holds no stream ({@link useVisibleTabCatchUp})
 * and refetches once when it becomes the visible tab again, under the same edit
 * guard as a frame. A surface that is always on screen leaves it `true`. A mount
 * owes no catch-up: the artifact query fetches on its own when the surface mounts,
 * so the latch starts `done` and only hiding the tab makes a read due. The
 * re-armed stream's first frame carries the whole body (`api_file_watch`
 * compares against an initial empty string), so a return also runs one
 * debounced refetch behind the catch-up; for an unchanged file it answers the
 * same artifact, which the query cache shares structurally, so nothing repaints.
 */
export function useArtifactLiveReload(
  slug: string | null | undefined,
  sourcePath: string | null | undefined,
  active = true,
): void {
  const queryClient = useQueryClient()
  const timerRef = useRef<ReturnType<typeof setTimeout>>()
  const latchRef = useRef<CatchUpLatch>('done')
  const watchPath = slug && sourcePath ? sourcePath : null

  const onFrame = useCallback(() => {
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

  const catchUp = useCallback(() => {
    if (!slug) return
    // An open edit buffer keeps its baseline here exactly as it does for a
    // frame; the latch stays due, and the editor's own refetch when editing
    // ends picks the change up.
    if (isArtifactEditing(slug)) { latchRef.current = 'due'; return }
    // The catch-up supersedes a frame refetch still waiting out its debounce.
    clearTimeout(timerRef.current)
    void queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      .then(() => settleCatchUp(latchRef))
  }, [queryClient, slug])

  // Cancel a pending refetch on unmount so it cannot fire into a torn-down tree.
  useEffect(() => () => clearTimeout(timerRef.current), [])

  useVisibleTabCatchUp({
    path: watchPath,
    active,
    watchArmable: true,
    catchUpArmable: !!watchPath,
    editing: false,
    latch: latchRef,
    catchUp,
    onFrame,
  })
}
