import type { ComponentType } from 'react'
import { Link2 } from 'lucide-react'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import JiraLogo from './icons/JiraLogo'
import { sourceProviderMeta } from '../utils/sourceProviderMeta'
import type { PullRequestProvider } from '../utils/pullRequestLinks'

type MarkComponent = ComponentType<{ size?: number; className?: string }>

/** The bundled logo per `SourceProviderMeta.logo` name. */
const BUNDLED_LOGOS: Readonly<Record<'github' | 'gitlab', MarkComponent>> = {
  github: GithubLogo,
  gitlab: GitlabLogo,
}

/**
 * The mark a provider draws itself, or null when it has none: its bundled logo
 * (named by `sourceProviderMeta(...).logo`), else a registered provider's own
 * `icon`. Jira is the one source with a bundled logo and no meta entry, since it
 * links issues only and the meta describes change providers.
 */
function providerMark(provider: PullRequestProvider): MarkComponent | null {
  if (provider === 'jira') return JiraLogo
  const meta = sourceProviderMeta(provider)
  if (meta.logo) return BUNDLED_LOGOS[meta.logo]
  return meta.icon ?? null
}

/**
 * The mark drawn beside a source link: the sidebar's session chips, the Changes
 * and Issues panels' tabs and headers, and the markdown forge chip all draw it
 * through this one component, so a provider wears the same mark on every surface
 * it appears on.
 *
 * The order is fixed (`providerMark`): a bundled logo, then a registered
 * provider's own `icon`, then ONE neutral glyph for every provider with neither
 * (Bitbucket Server, a registered provider that supplied no icon, or a provider
 * this build does not know). The neutral glyph is a plain link: it claims
 * neither a vendor's brand nor a kind of object, so it is equally true on a
 * change chip and on an issue chip, which carries its own issue dot beside it.
 *
 * Every mark is decorative (`aria-hidden`): the chip's text and tooltip carry
 * its name. A chip whose mark is the neutral glyph names its provider in that
 * tooltip instead (`sourceProviderHasMark`).
 */
export default function SourceProviderMark({
  provider,
  size,
}: {
  provider: PullRequestProvider
  size: number
}) {
  const Mark = providerMark(provider)
  if (Mark) return <Mark size={size} className="shrink-0" />
  // Sized by the attributes alone (no `lucide-inline`, whose `1em` box would
  // override them), so the glyph matches the logos beside it on every surface.
  return <Link2 size={size} className="shrink-0" aria-hidden="true" data-provider-mark="neutral" />
}

/** Whether a provider draws a mark of its own (a bundled logo or a registered
 *  icon) rather than the neutral glyph. A chip without one has nothing on it
 *  that says which provider it belongs to, so it names the provider in words. */
export function sourceProviderHasMark(provider: PullRequestProvider): boolean {
  return providerMark(provider) != null
}
