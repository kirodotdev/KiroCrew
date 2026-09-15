// Provider identity marks for a repo ref. On a mixed install a hard-coded
// GitHub logo is misleading: a GitLab project would render under a GitHub mark,
// and — worse — `group/project` on gitlab.com looks identical to the same path
// on a self-managed instance, so there would be no way to tell which server a
// row belongs to.
//
// Two pieces, deliberately separate:
//   ProviderLogo — the brand mark, wherever a repo is identified.
//   ProviderHostTag — the host, shown ONLY when it is not the provider's public
//     default. A "gitlab.com" chip on every GitLab row would be noise; a
//     "gitlab.acme.internal" chip is the only thing distinguishing two projects
//     that otherwise read the same.

import AzureDevopsLogo from '../../../components/icons/AzureDevopsLogo'
import GithubLogo from '../../../components/icons/GithubLogo'
import GitlabLogo from '../../../components/icons/GitlabLogo'
import { i18nT } from '../../../i18n/t'
import { type RepoRef } from '../api'
import { providerKeyOf, providerTerms } from '../lib/links'

/** Brand mark per provider id.
 *
 * A lookup rather than a ternary: `isGitlab ? GitlabLogo : GithubLogo` has two
 * arms, so a third provider silently renders under the GitHub mark — the exact
 * class of bug this component exists to prevent, just moved one provider along.
 * A record keyed by provider id fails to compile when a provider has no mark. */
const LOGO_BY_PROVIDER = {
  github: GithubLogo,
  gitlab: GitlabLogo,
  azure: AzureDevopsLogo,
} as const

/** The provider's brand mark for `repoRef`.
 *
 * A ref with no provider (a record persisted before multi-provider support)
 * renders the GitHub mark, which is what it is.
 */
export function ProviderLogo({
  repoRef,
  size = 18,
  className = '',
}: {
  repoRef?: Pick<RepoRef, 'provider'>
  size?: number
  className?: string
}) {
  const Logo = LOGO_BY_PROVIDER[providerKeyOf(repoRef)]
  const name = providerTerms(repoRef).providerName
  // The mark itself is `aria-hidden` (it is a CSS-mask span, or an inline SVG for
  // Azure DevOps), and the owner/repo text beside it does not say WHICH provider —
  // so the name is carried on a wrapper as an accessible label rather than being
  // dropped entirely.
  return (
    <span className="inline-flex flex-shrink-0" title={name} role="img" aria-label={name}>
      <Logo size={size} className={className} />
    </span>
  )
}

/** Public hosts, for which a host chip would be pure noise.
 *
 * `dev.azure.com` is the ONLY host Azure DevOps repos have here — Azure DevOps
 * Server (the self-hosted product) is a different URL shape the backend does not
 * accept — so without it every Azure row would grow a permanent
 * "dev.azure.com" chip claiming a self-managed instance that cannot exist. */
const DEFAULT_HOSTS = new Set(['github.com', 'gitlab.com', 'dev.azure.com', ''])

/** True when this ref lives on a self-managed instance worth naming.
 *
 * Tolerates an absent ref for the same reason `providerTerms` does: `host` is
 * already optional, so "absent" means the public default, and a component that
 * renders before the active repo resolves should show no chip rather than crash. */
export function hasCustomHost(repoRef?: Pick<RepoRef, 'host'>): boolean {
  return !DEFAULT_HOSTS.has((repoRef?.host || '').toLowerCase())
}

/** The host, as a small outlined chip — rendered only for a self-managed
 * instance, where it is the only thing that distinguishes this project from a
 * same-named one on the public site.
 *
 * Sized to stay within the line height (matching `ReadOnlyTag`) so a row does
 * not change height when the chip appears.
 */
export function ProviderHostTag({ repoRef }: { repoRef?: Pick<RepoRef, 'host'> }) {
  if (!hasCustomHost(repoRef)) return null
  return (
    <span
      className="flex-shrink-0 px-1.5 py-px rounded border border-border-strong text-[10px] leading-[14px] font-medium text-muted uppercase tracking-[.03em]"
      title={i18nT('apps.issueRadar.components.providerBadge.self_managed_instance', { host: repoRef?.host ?? '' })}
    >
      {repoRef?.host}
    </span>
  )
}
