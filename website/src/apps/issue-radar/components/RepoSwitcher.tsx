import { ChevronDown, Check } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuLabel,
} from '../../../components/ui/dropdown-menu'
import { ProviderLogo, ProviderHostTag } from './ProviderBadge'
import { useIssueRadar } from '../context'
import ReadOnlyTag, { isReadOnly } from './ReadOnlyTag'

import { i18nT } from '../../../i18n/t'
/** Prominent repo picker pinned to the TOP of the rail. Opens downward. Uses
 * the shared Radix DropdownMenu (never a native <select>) per product decision.
 * Shows the PROVIDER's brand mark, the owner/repo, a self-managed host chip when
 * there is one, and a small outlined "Read Only" tag for repos we lack write
 * access to (sized to stay within the line height so the row doesn't change
 * height when the tag appears/disappears).
 *
 * The provider mark and host chip are not decoration: `group/project` on
 * gitlab.com and on a self-managed instance are DIFFERENT projects that render
 * identically without them, so this is the only place the distinction is
 * visible. */
export default function RepoSwitcher() {
  const { repos, active, switchRepo } = useIssueRadar()
  // Matched on the full identity, not just owner/repo: on a mixed install the
  // same slug can exist on two providers, and matching loosely would badge the
  // active repo with the other one's permissions.
  const sameRepo = (r: { owner: string; repo: string; provider?: string; host?: string }) =>
    r.owner === active.owner
    && r.repo === active.repo
    && (r.provider || 'github') === (active.provider || 'github')
    && (r.host || 'github.com') === (active.host || 'github.com')
  const activeEntry = repos.find(sameRepo)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border border-border-strong bg-bg-elevated shadow-sm hover:border-accent hover:bg-bg-hover cursor-pointer outline-none transition-colors">
          <ProviderLogo repoRef={active} size={18} />
          <span className="flex-1 min-w-0 truncate text-[14px] font-semibold text-text text-left leading-5">
            {active.owner}/{active.repo}
          </span>
          <ProviderHostTag repoRef={active} />
          {isReadOnly(activeEntry?.permissions) && <ReadOnlyTag />}
          <ChevronDown size={15} className="text-muted flex-shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="bottom" sideOffset={6} className="w-[288px]">
        <DropdownMenuLabel className="text-[12px] uppercase tracking-[.04em]">{i18nT('apps.issueRadar.components.repoSwitcher.repositories')}</DropdownMenuLabel>
        {repos.map((r) => {
          const isActive = sameRepo(r)
          return (
            <DropdownMenuItem
              // Keyed on the full identity so two same-slug repos on different
              // providers are distinct rows rather than a React key collision.
              key={`${r.provider || 'github'}:${r.host || 'github.com'}:${r.owner}/${r.repo}`}
              onSelect={() => switchRepo({
                owner: r.owner,
                repo: r.repo,
                provider: r.provider,
                host: r.host,
              })}
            >
              <ProviderLogo repoRef={r} size={13} />
              <div className="flex-1 min-w-0 flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="truncate max-w-full">{r.owner}/{r.repo}</span>
                <ProviderHostTag repoRef={r} />
                {isReadOnly(r.permissions) && <ReadOnlyTag />}
              </div>
              {isActive && <Check size={13} className="text-accent flex-shrink-0" />}
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
