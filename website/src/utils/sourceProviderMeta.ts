/**
 * Presentation + capability lookup for the Changes panel, one entry per source
 * provider.
 *
 * The panel used to branch on `provider === 'github'` at roughly eight sites,
 * each independently deciding a label (`PR #12` vs `MR !12`), a logo, a display
 * name, or whether a write affordance exists. That shape has two problems: a
 * third provider is a boolean's worth of "not GitHub, so it must be GitLab", and
 * a fourth provider registered by a downstream edition would silently inherit
 * GitLab's punctuation and GitLab's write surface.
 *
 * So every site resolves ONE meta object instead. The two built-in entries below
 * reproduce today's rendering exactly — same strings, same catalog keys, same
 * affordances — and an edition-registered provider supplies its own through the
 * `SourceProviderDescriptor` it registered in `pullRequestLinks`.
 *
 * Deliberately free of JSX so it stays a util: a logo is named
 * (`'github' | 'gitlab' | null`) and the component is chosen by the renderer.
 *
 * `refLabel` resolves through the catalog rather than a template literal. `PR` and
 * `MR` are abbreviated WORDS, not punctuation, so they are copy: `#` and `!` are
 * the provider's own sigils and stay literal. The strings were untranslated
 * literals in `PullRequestPanel` before they were collected here, where the strict
 * i18n rule reaches inside module constants and surfaced them.
 */
import { i18nT } from '../i18n/t'
import {
  sourceProviderDescriptor,
  type PullRequestProvider,
  type SourceProviderCapabilities,
  type SourceProviderIcon,
} from './pullRequestLinks'

export interface SourceProviderMeta {
  id: string
  /** Provider product name for the panel header. A product name, not prose, so
   *  it is not translated — `'GitHub'` reads the same in every locale. */
  displayName: string
  /** Long reference, for tab strips and agent handoffs: `PR #12`, `CR-123`. */
  refLabel: (n: number) => string
  /** Short reference, for the title suffix where the title already precedes it:
   *  `#12`, `!12`, `CR-123`. */
  numberLabel: (n: number) => string
  /** Which bundled logo to draw, or null for a provider that ships none. */
  logo: 'github' | 'gitlab' | null
  /** A registered provider's own glyph component, carried verbatim from its
   *  descriptor. Checked by renderers BEFORE the `logo` name so an edition mark
   *  wins over the neutral fallback; built-ins never set it. Still "no JSX in
   *  this util": it is a component reference the renderer instantiates. */
  icon?: SourceProviderIcon
  /** True when this provider's objects are "pull requests" rather than "merge
   *  requests". Selects between the EXISTING catalog key pairs rather than
   *  introducing new strings; an unknown provider takes the pull-request
   *  wording, which is the more widely used term. */
  pullRequestWording: boolean
  capabilities: SourceProviderCapabilities
}

const GITHUB_META: SourceProviderMeta = {
  id: 'github',
  displayName: 'GitHub',
  refLabel: n => i18nT('components.pullRequestPanel.pr_number', { number: n }),
  numberLabel: n => `#${n}`,
  logo: 'github',
  pullRequestWording: true,
  capabilities: { checks: true, mergeState: true, resolveThreads: true, comment: true },
}

const GITLAB_META: SourceProviderMeta = {
  id: 'gitlab',
  displayName: 'GitLab',
  refLabel: n => i18nT('components.pullRequestPanel.mr_number', { number: n }),
  numberLabel: n => `!${n}`,
  logo: 'gitlab',
  pullRequestWording: false,
  // Checks and merge state are read for GitLab; the thread and comment WRITES
  // are GitHub-only in the gateway, which is why `CommentThreads` renders its
  // read-only notice for a merge request today. These flags encode that, so the
  // panel keeps rendering exactly what it renders now.
  capabilities: { checks: true, mergeState: true, resolveThreads: false, comment: false },
}

const BUILTIN_META: Readonly<Record<string, SourceProviderMeta>> = {
  github: GITHUB_META,
  gitlab: GITLAB_META,
}

/** Fail-closed defaults for a provider nothing has described: label it the way
 *  the widest number of forges do, draw no logo, and offer no write affordance.
 *  Reached only when a payload names a provider the frontend has no descriptor
 *  for — a backend/frontend registration mismatch — where rendering a button
 *  that can only fail is worse than rendering none. */
function fallbackMeta(provider: string): SourceProviderMeta {
  return {
    id: provider,
    // The provider's own identifier, like `source_ref_label` uses on the
    // backend: an id is not prose and has no translation.
    displayName: provider,
    refLabel: n => `#${n}`,
    numberLabel: n => `#${n}`,
    logo: null,
    pullRequestWording: true,
    capabilities: { checks: false, mergeState: false, resolveThreads: false, comment: false },
  }
}

/** Resolve the panel meta for a provider: built-in, then registered, then the
 *  fail-closed fallback. Built-ins are checked FIRST so a registration can never
 *  change how a core provider renders (registration also refuses their ids). */
export function sourceProviderMeta(provider: PullRequestProvider): SourceProviderMeta {
  const builtin = BUILTIN_META[provider]
  if (builtin) return builtin
  const descriptor = sourceProviderDescriptor(provider)
  if (!descriptor) return fallbackMeta(provider)
  const label = (n: number) => {
    try {
      const text = descriptor.refLabel(n)
      return typeof text === 'string' && text ? text : `#${n}`
    } catch {
      return `#${n}`
    }
  }
  return {
    id: descriptor.id,
    displayName: descriptor.displayName || descriptor.id,
    refLabel: label,
    numberLabel: label,
    logo: null,
    // A non-function `icon` is dropped rather than passed through: the sites
    // render `<meta.icon />`, and a bad value would throw at render — far from
    // the descriptor that supplied it.
    icon: typeof descriptor.icon === 'function' ? descriptor.icon : undefined,
    pullRequestWording: true,
    capabilities: descriptor.capabilities,
  }
}

/** Capability flags for a provider — the common case of needing only those. */
export function sourceProviderCapabilities(
  provider: PullRequestProvider,
): SourceProviderCapabilities {
  return sourceProviderMeta(provider).capabilities
}
