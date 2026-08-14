import { useQuery, skipToken } from '@tanstack/react-query'
import type { KiroPrerequisiteStatus } from '../api/client'

/** What the gateway host is, for copy that names an OS feature by its real name. */
export type GatewayPlatform = 'darwin' | 'windows' | 'other'

/**
 * The platform of the GATEWAY host, not of the browser.
 *
 * The browser's OS is the wrong signal for anything the gateway executes:
 * `/api/reveal` shells out on the gateway, so a dashboard opened from a Mac
 * against a Linux gateway must not name Finder. The install command has the same
 * property and is resolved server-side for the same reason.
 *
 * A pure reader — `skipToken` means this hook never fetches. The prerequisite
 * gate wraps the whole dashboard and owns that query, so the value is cached
 * before any page mounts, and this subscription re-renders when the gate
 * refreshes it.
 *
 * Everything unrecognised collapses to `'other'`, deliberately. The endpoint
 * reports the sentinel `'gateway'` to a non-owner dashboard user and to a probe
 * that could not run, and Linux has no single file manager to name, so both want
 * the same generic wording — naming an application we are not sure exists is the
 * failure mode worth designing out.
 */
export function useGatewayPlatform(): GatewayPlatform {
  const { data } = useQuery<KiroPrerequisiteStatus>({
    queryKey: ['kiro-prerequisite'],
    queryFn: skipToken,
  })
  const platform = data?.platform ?? ''
  if (platform === 'darwin') return 'darwin'
  // Matches the backend's own `sys.platform.startswith("win")` test, so the two
  // sides cannot disagree about what counts as Windows.
  if (platform.startsWith('win')) return 'windows'
  return 'other'
}
