import { useIssueRadar } from '../context'
import GeneralSettings from './settings/GeneralSettings'
import RepoSettings from './settings/RepoSettings'

/** Settings main area (full width). Routes between the shared General page
 * (account + connected-repo list) and a single repo's settings page, driven by
 * the rail's Settings section via `settingsTarget`. The rail stays visible. */
export default function SettingsView() {
  const { settingsTarget } = useIssueRadar()

  return (
    <div className="h-full overflow-y-auto bg-bg text-text scrollbar-none" style={{ scrollbarWidth: 'none' }}>
      {settingsTarget.kind === 'repo' ? (
        <RepoSettings
          key={`${settingsTarget.owner}/${settingsTarget.repo}`}
          owner={settingsTarget.owner}
          repo={settingsTarget.repo}
        />
      ) : (
        <GeneralSettings anchor={settingsTarget.anchor ?? 'account'} />
      )}
    </div>
  )
}
