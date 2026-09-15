import { queryOptions } from '@tanstack/react-query'
import type { ProviderAdapter } from '../providers/types'

const USAGE_REFRESH_MS = 5 * 60_000

/** Share the report between Overview and Usage for this dashboard's lifetime. */
export function providerUsageQuery(provider: ProviderAdapter) {
  return queryOptions({
    queryKey: ['provider-usage', provider.id],
    queryFn: () => provider.fetchUsage(),
    enabled: provider.capabilities.usageBilling,
    staleTime: USAGE_REFRESH_MS,
    gcTime: Infinity,
    refetchInterval: USAGE_REFRESH_MS,
    refetchIntervalInBackground: true,
  })
}
