import { ExternalLink, Info } from 'lucide-react'
import { Card } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'

export const REMOTE_CREW_WINDOWS_GUIDE_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/windows-install.md#launch-a-cloud-crew-from-windows'

/**
 * What the Set-up tab shows instead of the AWS checklist and the launch form when
 * the gateway runs on Windows. The dashboard launcher is POSIX-only there (the
 * server answers 400 `posix_host_required` before any AWS call), so the profile
 * was never read and nothing here may suggest it is wrong. It names the two ways
 * forward: the CLI launcher, which runs on Windows, and a gateway inside WSL.
 *
 * `error` is the rejected preflight when that refusal, not the platform signal,
 * is how the tab learned it: the sentence then goes through `ErrorNotice` so the
 * rejection keeps its report and hand-off.
 */
export function RemoteCrewWindowsNotice({ error }: { error?: unknown }) {
  const body = i18nT('pages.settings.remoteCrewPanel.windows_body')
  return (
    <Card>
      <section aria-labelledby="remote-crew-windows-title" className="space-y-2 text-[13px] text-text">
        <h3 id="remote-crew-windows-title" className="flex items-center gap-2 text-sm font-medium text-text-strong">
          <Info size={15} className="shrink-0 text-accent" aria-hidden="true" />
          {i18nT('pages.settings.remoteCrewPanel.windows_title')}
        </h3>
        {error === undefined
          ? <p>{body}</p>
          : <ErrorNotice message={body} report={reportForError(error)} askAgent />}
        <p>{i18nT('pages.settings.remoteCrewPanel.windows_cli')}</p>
        <p>{i18nT('pages.settings.remoteCrewPanel.windows_wsl')}</p>
        <a
          className="w-fit inline-flex items-center gap-1 text-accent font-medium hover:underline"
          href={REMOTE_CREW_WINDOWS_GUIDE_URL}
          target="_blank"
          rel="noreferrer"
        >
          {i18nT('pages.settings.remoteCrewPanel.windows_guide')} <ExternalLink size={12} aria-hidden="true" />
        </a>
      </section>
    </Card>
  )
}
