import { useConfigSchemaQuery } from '../../components/settingRef/useConfigSchema'
import { KIRO_SIGN_IN_BACKEND } from './kiroSignInLink'

/**
 * Whether this build offers Kiro sign-in, whichever harness is selected: the
 * schema's `agent.acp_backend` choices list the KAS backend. That list is the
 * build's and the deployment policy's answer (policy narrows it at boot), so a
 * build or fleet that cannot use the sign-in does not show it.
 *
 * `undefined` while the schema has not answered (loading or failed). A schema
 * with no choices for the field names no limit, so it offers the sign-in.
 */
export function useKiroSignInOffered(): boolean | undefined {
  return useKiroSignInOfferedQuery().offered
}

/**
 * The same answer with the schema read's failure exposed, for a caller that
 * must say so when the answer never comes (the sign-in notice: off KAS, a
 * failed schema read would otherwise leave the sign-in unchecked in silence).
 */
export function useKiroSignInOfferedQuery(): { offered: boolean | undefined; error: Error | null } {
  const { schema, error } = useConfigSchemaQuery()
  if (schema === undefined) return { offered: undefined, error }
  const values = schema.get('agent.acp_backend')?.enum
  return { offered: !values || values.includes(KIRO_SIGN_IN_BACKEND), error: null }
}
