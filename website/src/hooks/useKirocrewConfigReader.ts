import { useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'

/**
 * A one-shot reader of the gateway config body (`GET /api/config/kirocrew`)
 * that answers from the shared `['kirocrewConfig']` query.
 *
 * For code that needs the config inside a queryFn or a callback rather than as
 * a rendered value (the provider's default-effort resolver). `fetchQuery` serves
 * cached data when the key holds some, joins the GET an observer already has in
 * flight, and otherwise fetches and populates the key for every other reader.
 * Freshness is the key's own: the settings writes that change this body update
 * or invalidate `['kirocrewConfig']`, so a read here sees what they wrote.
 */
export function useKirocrewConfigReader(): () => Promise<unknown> {
  const queryClient = useQueryClient()
  return useCallback(
    () => queryClient.fetchQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() }),
    [queryClient],
  )
}
