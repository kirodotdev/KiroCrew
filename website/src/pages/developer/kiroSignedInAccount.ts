import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { failedWithNoData, failedWithNoReading } from '../../api/queryState'
import { isAccountOnly, parseKiroUsagePayload } from '../../api/kiroUsage'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../../components/KiroAccountModal'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { configuredAcpBackend, type AcpBackendConfig } from '../../api/acpBackend'
import { KIRO_SIGN_IN_BACKEND } from './kiroSignInLink'

/**
 * Who the stored sign-in belongs to (email, else account name), read from the
 * top bar's `['kiro-usage']` reading -- the vault stores no e-mail, so that
 * whoami-backed reading is the only place a name exists. That reading names the
 * vault account only while the saved main harness is KAS (the gateway's
 * `_vault_owns_usage_sync`); on any other harness it names kiro-cli's login,
 * which Crew's sign-in is not, so no name is given then, nor before the config
 * has loaded. `enabled: false`: the top bar owns both reads, this only observes
 * them. `hidden`: an e-mail follows the account popup's hide toggle (hidden by
 * default), so a shared screen does not show it either; an account name is
 * never hidden. `pending`: on KAS, the reading has not arrived yet (no answer, or
 * the gateway's cache is still cold) or the config that decides it is still
 * loading, so a name may still come; a failed read with no name to show is
 * settled, not pending, even after an earlier cold-cache reading and while
 * the next poll is in flight (`failedWithNoData`, not `isError`).
 */
export function useSignedInAccount(): { name: string; hidden: boolean; isEmail: boolean; pending: boolean } {
  const usageQuery = useQuery({
    queryKey: ['kiro-usage'],
    queryFn: () => api.sessionsUsage().then(parseKiroUsagePayload),
    enabled: false,
  })
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: false,
  })
  const { data } = usageQuery
  const cfg = cfgQuery.data
  // Failure is the last SETTLED outcome, never `isError`: a retry of a
  // data-less errored query goes back to `pending` with its error cleared, and
  // `pending` would flip on for every poll. A cold-cache `null` counts as no
  // name too: failed when an error settled after it.
  const usageFailed = failedWithNoReading(usageQuery)
  const [emailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  const none = { name: '', hidden: false, isEmail: false, pending: false }
  if (cfg === undefined) return { ...none, pending: !failedWithNoData(cfgQuery) }
  if (configuredAcpBackend(cfg) !== KIRO_SIGN_IN_BACKEND) return none
  if (data == null) return usageFailed ? none : { ...none, pending: true }
  if (typeof data !== 'object' || isAccountOnly(data)) return none
  if (data.email) return { name: data.email, hidden: emailHidden, isEmail: true, pending: false }
  return { ...none, name: data.account || '' }
}
