import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/** The core's AWS publish destination, as advertised by GET /api/publish-providers. */
const CORE_AWS_PROVIDER_ID = 'deploy-web-aws'
/** Its endpoint. Matched too, because the id alone is spoofable — see below. */
const CORE_AWS_ENDPOINT = '/api/deploy/deploy'

/**
 * Whether this installation may deploy to a public cloud URL.
 *
 * Derived from the publish-provider list rather than from a dedicated endpoint,
 * for two reasons. It is definitionally correct — if the core does not advertise
 * its AWS destination then there is nothing to offer, so no second source of
 * truth can drift from it. And the query key is the one `PublishHub` already
 * uses, so react-query serves both from a single request instead of adding a
 * fetch to every artifact card.
 *
 * Defaults to `true` while loading and on error: hiding a working affordance
 * because a list has not arrived yet is worse than briefly showing one, and the
 * backend refuses the action regardless.
 */
export function useCloudDeploymentEnabled(): boolean {
  const { data } = useQuery({
    queryKey: ['publish-providers'],
    queryFn: () => api.publishProviders(),
  })
  if (!data?.providers) return true
  // Both id AND endpoint must match. A provider id is a self-chosen string that any
  // app manifest can declare, so matching the id alone would let an installed app
  // claim `deploy-web-aws` and restore the UI the platform just withheld. The
  // endpoint is the core's own route, which an app-declared provider cannot own —
  // `collect_publish_providers` drops any app whose endpoint is not namespaced under
  // /api/apps/<that-app>/.
  return data.providers.some(
    p => p.id === CORE_AWS_PROVIDER_ID && p.endpoint === CORE_AWS_ENDPOINT,
  )
}
