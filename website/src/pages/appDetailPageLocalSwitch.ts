/**
 * The detail page's way on from an `app_trust_local_only` refusal.
 *
 * An app installed from a local folder holds a grant that covers only that
 * folder, so its registry update is refused with the repository the update
 * installs. The banner's button opens the consent dialog on that repository,
 * and confirming sends the registry update through the update route with the
 * repository as consent proof. The server switches the grant and puts the
 * local one back on failure, so the retry rejects on failure and the dialog's
 * not-saved state is the true one.
 */

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { appDisplayName } from '../components/appstore/appManifest'
import { backendRefusedNotice } from '../components/appstore/backendRefused'
import type { TrustAppTarget } from '../components/appstore/TrustAppModal'
import type { AppInfo } from './appDetailPageTypes'

export type LocalSwitchHandlers = {
  /** The page's trust gate `open`, which shows the consent dialog. */
  openTrust: (target: TrustAppTarget, retry: (name: string) => Promise<void>) => void
  /** Clear the refusal banner, reload the app and tell the other surfaces. */
  settle: () => Promise<void>
  /** Show the warning for a backend the host refused to start. */
  setBackendRefusedMsg: (msg: string) => void
  /** Show a transient success message. */
  showSuccess: (msg: string) => void
}

/** Open the consent dialog that trusts *repository* and updates *app* from it. */
export function openLocalSourceSwitch(app: AppInfo, repository: string, handlers: LocalSwitchHandlers): void {
  handlers.openTrust(
    {
      name: app.name,
      displayName: app.displayName,
      trustRepository: repository,
      // The badge names the listing, not the local install: the update records
      // the repository's copy as a registry install.
      origin: 'registry',
      _registry: app._registry,
      sessionApproval: app.manifest?.permissions?.sessionApproval === true,
      memberThreads: app.manifest?.permissions?.memberThreads === true,
      retryInstalls: true,
      replacesInstalled: true,
      switchesLocalSource: true,
    },
    async () => {
      const result = await api.updateApp(app.name, `registry:${app.name}`, repository) as
        { notice?: string; backend_refused?: string } | undefined
      await handlers.settle()
      // The same outcomes the Sync path reports: a backend the host refused is
      // a warning, and an update that newly asks for chat control leaves the
      // app disabled under its own persistent notice instead of a toast.
      const refused = backendRefusedNotice(appDisplayName(app), result)
      if (refused) handlers.setBackendRefusedMsg(refused)
      else if (result?.notice !== 'session_approval_reconsent') {
        handlers.showSuccess(i18nT('pages.appsPage.updated_from_its_repository', { name: appDisplayName(app) }))
      }
    },
  )
}
