import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import type { Artifact, ArtifactFolder } from '../types'
import { errMessage } from '../utils/thunkError'

/**
 * React-query plumbing for artifact-library folders.
 *
 * Query keys:
 *  - `['artifact-folders']` — the flat folder list (with per-folder item_count).
 *  - `['artifacts', ...]`   — existing artifact list queries; moves patch these
 *    optimistically so cards jump folders without a refetch flash.
 */

export function useArtifactFolders(): { folders: ArtifactFolder[]; isLoading: boolean } {
  const { data, isLoading } = useQuery<{ folders: ArtifactFolder[] }>({
    queryKey: ['artifact-folders'],
    queryFn: () => api.artifactFolders(),
  })
  return { folders: data?.folders ?? [], isLoading }
}

/** Invalidate everything folder membership touches (lists + counts). */
export function useInvalidateArtifactFolders(): () => void {
  const qc = useQueryClient()
  return useCallback(() => {
    qc.invalidateQueries({ queryKey: ['artifact-folders'] })
    qc.invalidateQueries({ queryKey: ['artifacts'] })
  }, [qc])
}

/** Options for a single artifact move. */
export type MoveArtifactOptions = {
  /**
   * Called once the server has ACKNOWLEDGED this move.
   *
   * The optimistic write patches the cache immediately, which is not the same
   * fact: a caller that treats the cache as proof (the drag-move undo bar)
   * would offer an undo before the original move is durable, and the two
   * writes can then race and cancel each other. Mirrors `MoveSlotOptions`.
   */
  onCommitted?: () => void
}

/** The newest move asked for per slug, shared by every hook instance (the
 *  library page and the detail header both move artifacts), so a failed move
 *  that a later one superseded neither rolls back nor reports. */
const latestMove = new Map<string, number>()
let moveSeq = 0
/** Moves still in flight per slug, and whether any two overlapped. Overlapping
 *  requests can land on the server out of order, so the caches are reconciled
 *  with the server once the LAST one settles, whichever move that is. */
const inFlight = new Map<string, { count: number; overlapped: boolean }>()

/**
 * Move an artifact into a folder (`''` = unfile to root) with optimistic
 * cache updates: every cached `['artifacts', ...]` list gets the artifact's
 * `folder_id` patched immediately; a failure rolls back via invalidation.
 * Mirrors `useMoveSlotToFolder` (the chat-sidebar precedent).
 */
export function useMoveArtifactToFolder(): (slug: string, folderId: string, opts?: MoveArtifactOptions) => void {
  const qc = useQueryClient()
  const dispatch = useAppDispatch()
  const { mutate } = useMutation({
    mutationFn: ({ slug, folderId }: { slug: string; folderId: string; onCommitted?: () => void }) =>
      api.setArtifactFolder(slug, folderId),
    onMutate: ({ slug, folderId }) => {
      const move = ++moveSeq
      latestMove.set(slug, move)
      const pending = inFlight.get(slug)
      inFlight.set(slug, pending ? { count: pending.count + 1, overlapped: true } : { count: 1, overlapped: false })
      qc.setQueriesData<{ artifacts: Artifact[] }>({ queryKey: ['artifacts'] }, (old) => {
        if (!old?.artifacts) return old
        return {
          ...old,
          artifacts: old.artifacts.map(a => (a.slug === slug ? { ...a, folder_id: folderId } : a)),
        }
      })
      qc.setQueryData<Artifact>(['artifact', slug], (old) =>
        old ? { ...old, folder_id: folderId } : old,
      )
      return { move }
    },
    // The ack rides the mutation VARIABLES: TanStack Query's observer only
    // invokes the LATEST call's per-call callbacks, so a per-call
    // `mutate(..., { onSuccess })` would drop the ack whenever a second move
    // started before the first settled — and the first offer never goes live.
    onSuccess: (_data, vars) => vars.onCommitted?.(),
    onError: (err, { slug }, ctx) => {
      // A move a later one superseded rolls nothing back: a refetch now would
      // read the server before the later move lands and drop the card back
      // into its old folder, with no notice and no later refetch to fix it.
      if (!ctx || latestMove.get(slug) !== ctx.move) return
      // Rollback by refetch — cheaper and safer than replaying prior snapshots
      // across every cached list variant.
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      // The artifact just jumped back to where it was: say so, with the
      // gateway's reason, on the same shell banner a refused session move uses.
      const title = i18nT('hooks.useArtifactFolders.move_refused')
      const reason = errMessage(err)
      dispatch(setAgentSwitchNotice(reason ? { title, message: reason, error: true } : { message: title, error: true }))
    },
    onSettled: (_data, _err, { slug }) => {
      qc.invalidateQueries({ queryKey: ['artifact-folders'] })
      const pending = inFlight.get(slug)
      if (pending && pending.count > 1) {
        inFlight.set(slug, { ...pending, count: pending.count - 1 })
        return
      }
      inFlight.delete(slug)
      // The last move for this slug has settled, so the server now holds the
      // final folder: reconcile the detail, and the lists too when moves
      // overlapped, since an older request may have landed after a newer one.
      qc.invalidateQueries({ queryKey: ['artifact', slug] })
      if (pending?.overlapped) qc.invalidateQueries({ queryKey: ['artifacts'] })
    },
  })
  // `mutate` is referentially stable across renders, so the returned callback is too.
  return useCallback((slug: string, folderId: string, opts?: MoveArtifactOptions) => {
    mutate({ slug, folderId, onCommitted: opts?.onCommitted })
  }, [mutate])
}
