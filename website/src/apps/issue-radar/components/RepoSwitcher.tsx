import { ChevronDown, Check } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuLabel,
} from '../../../components/ui/dropdown-menu'
import { ProviderLogo, ProviderHostTag } from './ProviderBadge'
import { useIssueRadar } from '../context'
import ReadOnlyTag, { isReadOnly } from './ReadOnlyTag'

import { i18nT } from '../../../i18n/t'

/** Left-to-right mark, U+200E. See RepoPathLabel. */
const LRM = '\u200E'

/** One repo path, rendered repo-name-first: the owner/group path is muted and
 * truncates from the LEFT (ellipsis prefix), the repo name never gives up its
 * width to the owner path.
 *
 * Why: GitLab nests groups arbitrarily deep (`acme/infra/cloud/modules/…`), and
 * with plain right-side truncation every repo under the same parent group
 * renders identically — the distinguishing repo name is exactly the part that
 * gets cut off (#3047). Left-truncation keeps the tail of the group path (the
 * most specific part) and the whole repo name visible.
 *
 * Mechanics: the owner span is `dir="rtl"`, which moves its CSS ellipsis to the
 * left edge; LRM (U+200E) sentinels keep the characters themselves in logical
 * order when the path starts or ends on a neutral/weak character (digits, `-`).
 * Each sentinel sits in its own `aria-hidden` + `select-none` span, so it still
 * participates in bidi resolution but never lands in a text selection, the
 * clipboard, or the accessible name. The owner span carries a huge flex-shrink
 * and a zero min-width so it absorbs ALL width pressure before the repo span
 * (plain `truncate`) gives up a single pixel. Deliberately NO min-width floor
 * on the owner span: a floor only takes effect when the repo name already
 * needs every pixel, so it would re-truncate the repo name (the #3047 defect)
 * to show an ellipsis that carries less information than the repo characters
 * it displaced — verified empirically at 288px with this issue's repro paths.
 * When the owner collapses fully, the leading `/` on the repo span still
 * signals a dropped prefix, and the full path stays one hover away in `title`.
 */
function RepoPathLabel({
  owner, repo, className = '', repoClassName = '',
}: {
  owner: string
  repo: string
  className?: string
  repoClassName?: string
}) {
  return (
    <span
      className={`min-w-0 flex items-baseline ${className}`}
      title={`${owner}/${repo}`}
      data-testid="repo-path-label"
    >
      <span dir="rtl" className="min-w-0 [flex-shrink:9999] truncate text-muted font-normal">
        <span aria-hidden="true" className="select-none">{LRM}</span>
        {owner}
        <span aria-hidden="true" className="select-none">{LRM}</span>
      </span>
      <span className={`min-w-0 truncate ${repoClassName}`}>/{repo}</span>
    </span>
  )
}

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
        <button className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border border-border-strong bg-bg-elevated shadow-sm hover:border-accent hover:bg-bg-hover cursor-pointer transition-colors">
          <ProviderLogo repoRef={active} size={18} />
          <RepoPathLabel
            owner={active.owner}
            repo={active.repo}
            className="flex-1 text-[14px] text-left leading-5"
            repoClassName="font-semibold text-text"
          />
          <ProviderHostTag repoRef={active} />
          {isReadOnly(activeEntry?.permissions) && <ReadOnlyTag repoRef={activeEntry} />}
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
                <RepoPathLabel
                  owner={r.owner}
                  repo={r.repo}
                  className="max-w-full"
                  repoClassName="font-medium"
                />
                <ProviderHostTag repoRef={r} />
                {isReadOnly(r.permissions) && <ReadOnlyTag repoRef={r} />}
              </div>
              {isActive && <Check size={13} className="text-accent flex-shrink-0" />}
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
