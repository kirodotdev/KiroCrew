import { User, Library, Plus, type LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { ProviderLogo } from './ProviderBadge'
import { type RepoRef } from '../api'
import { useIssueRadar } from '../context'
import type { GeneralAnchor } from '../lib/types'

/** Body of the "Settings" accordion section. Two groups:
 *   • General — account + the connected-repo list (app-wide).
 *   • Repository settings — one row per connected repo, each opening that
 *     repo's own settings page (triage labels, good-first-issue labels, …).
 * Every row navigates the main area via `openSettings(target)`. */
export default function SettingsSection() {
  const { repos, mainView, settingsTarget, openSettings, onAddRepo } = useIssueRadar()
  const inSettings = mainView === 'settings'

  const generalItems: { key: GeneralAnchor; label: string; icon: LucideIcon }[] = [
    { key: 'account', label: 'Account', icon: User },
    { key: 'repos', label: 'Repositories', icon: Library },
  ]

  return (
    <div className="px-3 pt-1 pb-1 flex flex-col gap-3">
      <div>
        <GroupLabel>General</GroupLabel>
        <div className="flex flex-col gap-0.5">
          {generalItems.map((g) => (
            <NavItem
              key={g.key}
              icon={g.icon}
              label={g.label}
              active={inSettings && settingsTarget.kind === 'general' && (settingsTarget.anchor ?? 'account') === g.key}
              onClick={() => openSettings({ kind: 'general', anchor: g.key })}
            />
          ))}
        </div>
      </div>

      <div>
        <GroupLabel>Repository settings</GroupLabel>
        <div className="flex flex-col gap-0.5">
          {repos.map((r) => (
            <NavItem
              key={`${r.owner}/${r.repo}`}
              repoRef={r}
              label={`${r.owner}/${r.repo}`}
              active={
                inSettings && settingsTarget.kind === 'repo'
                && settingsTarget.owner === r.owner && settingsTarget.repo === r.repo
              }
              onClick={() => openSettings({ kind: 'repo', owner: r.owner, repo: r.repo, provider: r.provider, host: r.host })}
            />
          ))}
          <button
            onClick={onAddRepo}
            className="mt-0.5 w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent"
          >
            <Plus size={14} className="flex-shrink-0" />
            <span className="flex-1">Connect repo</span>
          </button>
        </div>
      </div>
    </div>
  )
}

function NavItem({ icon: Icon, repoRef, label, active, onClick }: {
  // `repoRef` replaces the old `github` boolean: a repo row now shows ITS OWN
  // provider mark, which a boolean could not express once there were two.
  icon?: LucideIcon; repoRef?: Pick<RepoRef, 'provider'>; label: string
  active: boolean; onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left cursor-pointer transition-colors ${
        active ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover'
      }`}
    >
      {repoRef
        ? <ProviderLogo repoRef={repoRef} size={14} />
        : Icon
          ? <Icon size={14} className={`flex-shrink-0 ${active ? 'text-accent' : ''}`} />
          : null}
      <span className="flex-1 truncate">{label}</span>
    </button>
  )
}

function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 pb-1 text-[10px] font-semibold text-muted uppercase tracking-[.08em] opacity-70">
      {children}
    </div>
  )
}
