import { User, Library, Plus, type LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { ProviderLogo } from './ProviderBadge'
import { type RepoRef } from '../api'
import { useIssueRadar } from '../context'
import { repoScopeKey, sameRepoRef } from '../lib/links'
import type { GeneralAnchor } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** Body of the "Settings" accordion section. Two groups:
 *   • General — account + the connected-repo list (app-wide).
 *   • Repository settings — one row per connected repo, each opening that
 *     repo's own settings page (triage labels, good-first-issue labels, …).
 * Every row navigates the main area via `openSettings(target)`. */
/** `onNavigate` fires after any navigation so a narrow viewport can collapse the
 * full-width rail — otherwise the tap navigates behind a rail still covering it. */
export default function SettingsSection({ onNavigate }: { onNavigate?: () => void }) {
  const { repos, mainView, settingsTarget, openSettings, onAddRepo } = useIssueRadar()
  const inSettings = mainView === 'settings'

  const generalItems: { key: GeneralAnchor; label: string; icon: LucideIcon }[] = [
    { key: 'account', label: 'Account', icon: User },
    { key: 'repos', label: 'Repositories', icon: Library },
  ]

  return (
    <div className="px-3 pt-1 pb-1 flex flex-col gap-3">
      <div>
        <GroupLabel>{i18nT('apps.issueRadar.components.settingsSection.general')}</GroupLabel>
        <div className="flex flex-col gap-0.5">
          {generalItems.map((g) => (
            <NavItem
              key={g.key}
              icon={g.icon}
              label={g.label}
              active={inSettings && settingsTarget.kind === 'general' && (settingsTarget.anchor ?? 'account') === g.key}
              onClick={() => { openSettings({ kind: 'general', anchor: g.key }); onNavigate?.() }}
            />
          ))}
        </div>
      </div>

      <div>
        <GroupLabel>{i18nT('apps.issueRadar.components.settingsSection.repository_settings')}</GroupLabel>
        <div className="flex flex-col gap-0.5">
          {repos.map((r) => (
            <NavItem
              // Keyed on the full identity, not the slug: on a mixed install one
              // slug can exist on two forges, and a slug key would be DUPLICATE
              // across those two rows.
              key={repoScopeKey(r)}
              repoRef={r}
              label={`${r.owner}/${r.repo}`}
              active={
                // Full identity again, for the same reason: matched on the slug,
                // both rows of a mixed-forge slug light up as the open page.
                inSettings && settingsTarget.kind === 'repo'
                && sameRepoRef(settingsTarget, r)
              }
              onClick={() => { openSettings({ kind: 'repo', owner: r.owner, repo: r.repo, provider: r.provider, host: r.host }); onNavigate?.() }}
            />
          ))}
          <button
            onClick={onAddRepo}
            className="mt-0.5 w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent"
          >
            <Plus size={14} className="flex-shrink-0" />
            <span className="flex-1">{i18nT('apps.issueRadar.components.settingsSection.connect_repo')}</span>
          </button>
        </div>
      </div>
    </div>
  )
}

function NavItem({ icon: Icon, repoRef, label, active, onClick }: {
  // `repoRef` carries each row's provider so it shows ITS OWN provider mark;
  // a plain boolean cannot express the provider when there is more than one.
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
