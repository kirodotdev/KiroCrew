/**
 * Whether the composer may offer `Auto (Jev)` on its split send button.
 *
 * Two answers, both the gateway's, and BOTH are required — the same split the
 * Decisions card in Settings reads (`decisionsPreview.ts` explains why the two
 * live in different places):
 *
 * - `decisions_enabled` on `GET /api/dashboard/config` is the FLEET's ceiling
 *   (`capabilities.decisions`), resolved server-side;
 * - `permits` on `GET /api/decisions/consent` is the OWNER's keystone consent,
 *   bound to the endpoint the config names now.
 *
 * Fail CLOSED on `=== true`, the posture `socialShareOn` uses: an absent field, an
 * older gateway, a 404 on the consent route, a read that has not landed, and any
 * failure all withhold the mode. Offering it anyway would put a third entry in the
 * picker whose send the gateway refuses to decide — a steer either way, but one the
 * sender was told was a decision.
 *
 * The two query keys are the ones the settings card already uses, so a composer
 * mounted beside an open Settings page shares both cached reads rather than
 * issuing its own.
 */
import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'

export function useJevAutoSend(): boolean {
  const dashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const consentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    // A 404 is the answer "this gateway predates the keystone", not a transient
    // failure worth retrying — and it withholds the mode either way.
    retry: false,
    staleTime: 30_000,
  })
  // `permits` is the server's own verdict (consented AND the recorded endpoint is
  // the configured one). Read it rather than re-deriving the comparison here, so
  // the picker and the gate cannot disagree about whether anything would be asked.
  return dashCfgQ.data?.decisions_enabled === true && consentQ.data?.permits === true
}
