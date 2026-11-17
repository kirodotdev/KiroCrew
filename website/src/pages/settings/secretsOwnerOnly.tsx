import StatusNotice from '../../components/StatusNotice'
import ErrorNotice from '../../components/ErrorNotice'
import { reportForError } from '../../utils/errorReport'
import { apiErrorCode } from '../../api/apiError'
import { useViewerIsNotOwner } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'

/** Why this viewer gets a notice in place of the Secrets page, if they do. */
export type SecretsHeld = 'teammate' | 'relay' | null

/**
 * Whether the Secrets page is held for this viewer, and why. A teammate is
 * held because the vault holds credentials that would outlive their access.
 * Anyone else the relay refuses the vault to (the owner on Remote Control
 * included) is held because the relay does not carry local credentials. Either
 * way they get one notice in place of a failed list, an Add button that cannot
 * save and a command for a shell they cannot reach.
 */
export function useSecretsOwnerOnly(listError: unknown): SecretsHeld {
  const { notOwner } = useViewerIsNotOwner()
  if (notOwner) return 'teammate'
  return apiErrorCode(listError) === 'relay_local_credential' ? 'relay' : null
}

/** The teammate's is a capability notice; the relay's is the list read's
 *  refusal, so it renders as that error with its report. */
export function SecretsOwnerOnlyNotice({ held, error }: { held: Exclude<SecretsHeld, null>; error?: unknown }) {
  return held === 'teammate'
    ? <StatusNotice tone="info" testId="secrets-owner-only" message={i18nT('settings.secrets.owner_only')} />
    : (
      <ErrorNotice
        testId="secrets-relay-unavailable"
        message={i18nT('settings.secrets.relay_unavailable')}
        report={reportForError(error)}
        askAgent
      />
    )
}
