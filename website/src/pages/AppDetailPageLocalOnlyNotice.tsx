/**
 * The App Store detail page's error notice for an `app_trust_local_only` refusal.
 *
 * It explains the refusal in plain words and keeps the way forward, "Trust the
 * repository and update", in the notice footer.
 */
import { ShieldCheck } from 'lucide-react'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { i18nT } from '../i18n/t'
import type { ErrorReport } from '../utils/errorReport'

export default function AppDetailPageLocalOnlyNotice({
  name,
  repository,
  report,
  busy,
  onTrust,
  onDismiss,
}: {
  /** The app's display name, already localised. */
  name: string
  /** The repository the refused update installs from. */
  repository: string
  /** The journaled refusal report, when one was recorded. */
  report: ErrorReport | undefined
  /** An action is running, so the button is disabled. */
  busy: boolean
  onTrust: () => void
  onDismiss: () => void
}) {
  return (
    <ErrorNotice
      message={i18nT('pages.appDetailPage.local_only_refusal', { app: name, repository })}
      report={report}
      askAgent
      onDismiss={onDismiss}
      footer={(
        <Btn onClick={onTrust} disabled={busy}>
          <ShieldCheck size={14} /> {i18nT('pages.appDetailPage.trust_repository_and_update')}
        </Btn>
      )}
      className="mb-4 animate-rise"
    />
  )
}
