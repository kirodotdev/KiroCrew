import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
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
 * never hidden.
 */
export function useSignedInAccount(): { name: string; hidden: boolean; isEmail: boolean } {
  const { data } = useQuery({
    queryKey: ['kiro-usage'],
    queryFn: () => api.sessionsUsage().then(parseKiroUsagePayload),
    enabled: false,
  })
  const { data: cfg } = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: false,
  })
  const [emailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  const none = { name: '', hidden: false, isEmail: false }
  if (cfg === undefined || configuredAcpBackend(cfg) !== KIRO_SIGN_IN_BACKEND) return none
  if (typeof data !== 'object' || data === null || isAccountOnly(data)) return none
  if (data.email) return { name: data.email, hidden: emailHidden, isEmail: true }
  return { ...none, name: data.account || '' }
}
