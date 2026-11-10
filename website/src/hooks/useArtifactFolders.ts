import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { useAppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'
import type { Artifact, ArtifactFolder } from '../types'

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

/* The latest move attempt per artifact, shared by every hook instance (the
 * library page and the detail header), so a refusal can tell whether a newer
 * move of the same artifact was made after it. In memory only. */
let moveSeq = 0
const latestMove = new Map<string, number>()

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
      const seq = ++moveSeq
      latestMove.set(slug, seq)
      return { seq }
    },
    // The ack rides the mutation VARIABLES: TanStack Query's observer only
    // invokes the LATEST call's per-call callbacks, so a per-call
    // `mutate(..., { onSuccess })` would drop the ack whenever a second move
    // started before the first settled — and the first offer never goes live.
    onSuccess: (_data, vars) => vars.onCommitted?.(),
    onError: (err, { slug }, ctx) => {
      // A newer move of this artifact was made after this one: the refusal is
      // stale. Skip both the rollback and the notice. The newer move owns the
      // cache: its success keeps its optimistic write, and its own failure rolls
      // back. Refetching here could land the old folder over a newer move still
      // in flight, and "not moved" would contradict the move the user made last.
      // Order comes from the attempt sequence, never the cache, which a refetch
      // can rewrite while the request is in flight.
      if (ctx && latestMove.get(slug) !== ctx.seq) return
      // Rollback by refetch — cheaper and safer than replaying prior snapshots
      // across every cached list variant.
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      // The artifact is about to jump back: say so on the shell banner, with the
      // gateway's reason, or the rollback reads as the move never having been
      // asked for. Same notice the session move uses.
      const title = i18nT('hooks.useMoveArtifactToFolder.move_refused')
      const reason = errMessage(err)
      dispatch(setAgentSwitchNotice(reason ? { title, message: reason, error: true } : { message: title, error: true }))
    },
    onSettled: (_data, _err, { slug }, ctx) => {
      qc.invalidateQueries({ queryKey: ['artifact-folders'] })
      // A superseded attempt leaves the detail cache to the newer move, for the
      // same reason onError skips its rollback.
      if (ctx && latestMove.get(slug) !== ctx.seq) return
      if (ctx) latestMove.delete(slug)
      qc.invalidateQueries({ queryKey: ['artifact', slug] })
    },
  })
  // `mutate` is referentially stable across renders, so the returned callback is too.
  return useCallback((slug: string, folderId: string, opts?: MoveArtifactOptions) => {
    mutate({ slug, folderId, onCommitted: opts?.onCommitted })
  }, [mutate])
}
