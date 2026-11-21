/** The owner's memory provider preview switch (`/api/memory/provider-preview`).
 *
 *  Shaped like the Shared Crew switch (`sharedCrewPreviewSetting.ts`): the stored
 *  choice applies on the next gateway start. `active` is what this gateway runs, so
 *  every memory provider surface (the Settings picker, the sheets an app opens)
 *  reads it here. A refusal or an older gateway without the route reads as off; any
 *  other failed read is `failed`, which no surface may take for off. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'

export const memoryProviderPreviewKey = ['memoryProvider', 'preview'] as const

function isRefusal(err: unknown): boolean {
  const status = (err as { status?: unknown } | null)?.status
  return status === 403 || status === 404
}

export function useMemoryProviderPreviewSetting({ poll = false }: { poll?: boolean } = {}) {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: memoryProviderPreviewKey,
    queryFn: () => api.memoryProviderPreview(),
    retry: false,
    refetchInterval: poll ? 1000 : false,
  })
  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.memoryProviderSetPreview(enabled),
    onSuccess: data => { qc.setQueryData(memoryProviderPreviewKey, data) },
  })
  return {
    setting: query.data ?? null,
    settled: query.isSuccess || query.isError,
    visible: !!query.data || (query.isError && !isRefusal(query.error)),
    /** The read failed for a reason other than a refusal: on or off is unknown. */
    failed: query.isError && !isRefusal(query.error),
    loadError: !poll && query.isError && !isRefusal(query.error),
    set: (enabled: boolean) => mutation.mutate(enabled),
    saving: mutation.isPending,
    saveFailed: mutation.isError,
  }
}

/** Whether an app page shows the `memoryAdmin` grant: the preview runs here, or the
 *  read failed, since an unknown answer keeps the grant listed for the owner to see. */
export function useMemoryProviderGrantShown(): boolean {
  const preview = useMemoryProviderPreviewSetting()
  return !!preview.setting?.active || preview.failed
}
