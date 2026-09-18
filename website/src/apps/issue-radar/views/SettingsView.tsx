import { useIssueRadar } from '../context'
import { repoScopeKey } from '../lib/links'
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
        // Key by the FULL provider-qualified scope, not owner/repo alone: the same
        // slug can exist on two providers (github.com vs a GitLab instance), and a
        // bare owner/repo key would reuse this instance across that switch. Its
        // persistent draft/revision/dirtyKeys refs would then carry repo A's
        // pending edit into a write to repo B — silent cross-repo corruption.
        <RepoSettings
          key={repoScopeKey(settingsTarget)}
          repoRef={settingsTarget}
        />
      ) : (
        <GeneralSettings anchor={settingsTarget.anchor ?? 'account'} />
      )}
    </div>
  )
}
