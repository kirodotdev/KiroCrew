/** The owner's Shared Crew preview switch (`/api/shared-crew/preview`).
 *
 *  The stored choice only takes effect when the gateway restarts, so the answer
 *  carries three more facts: what this gateway started with (`active`), whether
 *  the environment forces the preview on (`env_override`) and whether a restart
 *  would change anything (`restart_required`). A gateway without the route and a
 *  caller who is not the owner both read as "no card". */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'

export const sharedCrewPreviewKey = [...sharedCrewKeys.all, 'preview'] as const

/** An owner-only refusal, which hides the card instead of showing an error. */
function isRefusal(err: unknown): boolean {
  const status = (err as { status?: unknown } | null)?.status
  return status === 403 || status === 404
}

export function useSharedCrewPreviewSetting({ poll = false }: { poll?: boolean } = {}) {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: sharedCrewPreviewKey,
    queryFn: () => api.sharedCrewPreview(),
    retry: false,
    refetchInterval: poll ? 1000 : false,
  })
  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.sharedCrewSetPreview(enabled),
    onSuccess: data => { qc.setQueryData(sharedCrewPreviewKey, data) },
  })
  return {
    setting: query.data ?? null,
    // Drawn once the answer is in: a card with a stand-in switch would flash
    // for a caller who is not the owner.
    visible: !!query.data || (query.isError && !isRefusal(query.error)),
    // A failed poll during a restart keeps the last answer; the restart timeout reports it.
    loadError: !poll && query.isError && !isRefusal(query.error),
    refetch: query.refetch,
    set: (enabled: boolean) => mutation.mutate(enabled),
    saving: mutation.isPending,
    saveFailed: mutation.isError,
  }
}
