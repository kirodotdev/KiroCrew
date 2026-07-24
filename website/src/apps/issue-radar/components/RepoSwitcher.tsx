import { ChevronDown, Check } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuLabel,
} from '../../../components/ui/dropdown-menu'
import GithubLogo from '../../../components/icons/GithubLogo'
import { useIssueRadar } from '../context'
import ReadOnlyTag, { isReadOnly } from './ReadOnlyTag'

/** Prominent repo picker pinned to the TOP of the rail. Opens downward. Uses
 * the shared Radix DropdownMenu (never a native <select>) per product decision.
 * Shows a GitHub logo, the owner/repo, and a small outlined "Read Only" tag for
 * repos we lack write access to (sized to stay within the line height so the
 * row doesn't change height when the tag appears/disappears). */
export default function RepoSwitcher() {
  const { repos, active, switchRepo } = useIssueRadar()
  const activeEntry = repos.find((r) => r.owner === active.owner && r.repo === active.repo)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border border-border-strong bg-bg-elevated shadow-sm hover:border-accent hover:bg-bg-hover cursor-pointer outline-none transition-colors">
          <GithubLogo size={18} className="flex-shrink-0" />
          <span className="flex-1 min-w-0 truncate text-[14px] font-semibold text-text text-left leading-5">
            {active.owner}/{active.repo}
          </span>
          {isReadOnly(activeEntry?.permissions) && <ReadOnlyTag />}
          <ChevronDown size={15} className="text-muted flex-shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="bottom" sideOffset={6} className="w-[288px]">
        <DropdownMenuLabel className="text-[12px] uppercase tracking-[.04em]">Repositories</DropdownMenuLabel>
        {repos.map((r) => {
          const isActive = r.owner === active.owner && r.repo === active.repo
          return (
            <DropdownMenuItem
              key={`${r.owner}/${r.repo}`}
              onSelect={() => switchRepo({ owner: r.owner, repo: r.repo })}
            >
              <GithubLogo size={13} className="flex-shrink-0" />
              <div className="flex-1 min-w-0 flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="truncate max-w-full">{r.owner}/{r.repo}</span>
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
