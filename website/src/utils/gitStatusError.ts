import { parseErrorCode } from './errorReport'

/**
 * Readers for the `['git-status', …]` / `['git-log', …]` 503 bodies.
 *
 * FOUR components render those two queries, and the filter-driver refusal is a
 * standing policy decision rather than an outage: permanent for the repository,
 * with a knowable cause, and no retry clears it. Any surface that spells it as a
 * generic failure tells an LFS user their repository is broken on every poll,
 * forever -- which is the defect this endpoint's refusal codes exist to end. So
 * the recognition lives here once instead of being re-derived per component.
 */

/** Machine-readable code from an ApiError-shaped query failure. */
export function gitErrorCode(error: unknown): string | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const body = (error as { body?: unknown }).body
  return typeof body === 'string' ? parseErrorCode(body) : undefined
}

/** True when this failure is the repo-config filter refusal, on either route. */
export function isGitFilterRefusal(error: unknown): boolean {
  const code = gitErrorCode(error)
  return code === 'git_status_filter_refused' || code === 'git_log_filter_refused'
}

/**
 * The refusal's `cause` discriminator: `declared` when repo config names a
 * filter driver, `unreadable` when the config probe could not prove one absent.
 * One code, because it is one condition (refused by policy) -- but the reader is
 * told which of the two facts holds instead of both at once.
 */
export function gitFilterRefusalCause(error: unknown): string | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const body = (error as { body?: unknown }).body
  if (typeof body !== 'string' || !body.trim().startsWith('{')) return undefined
  try {
    const parsed = JSON.parse(body) as { cause?: unknown }
    return typeof parsed.cause === 'string' && parsed.cause ? parsed.cause : undefined
  } catch {
    return undefined
  }
}

/**
 * The i18n key for the refusal copy this cause supports. An unknown or absent
 * cause falls back to the unreadable sentence, which claims strictly less: it
 * names no driver, and promises nothing about permanence.
 *
 * The body states the CAUSE and what to do about it. What cannot be shown is
 * the title's job ({@link gitFilterRefusalTitleKey}), because that part varies
 * with how many routes are refusing while the cause does not.
 */
export function gitFilterRefusalCopyKey(cause: string | undefined): string {
  return cause === 'declared'
    ? 'components.gitPanel.filter_refused'
    : 'components.gitPanel.filter_refused_unreadable'
}

/**
 * How much of the panel a refusal actually accounts for.
 *
 * `both` when both routes refuse, and one half otherwise. The refusal is
 * repo-level so the routes normally refuse together, but they need not: a
 * corrupt HEAD fails the status route on its own terms while the log route
 * still refuses, and a status route that has recovered leaves a cached log
 * refusal standing on its own. In those states a refusal that claims both
 * halves is reporting a sibling notice's failure as well as its own -- or, when
 * the sibling route is simply healthy, claiming a list the panel is rendering
 * underneath it cannot be shown.
 */
export type GitFilterRefusalScope = 'both' | 'changes' | 'history'

/**
 * Titles for the refusal, by cause and by scope.
 *
 * Written out as a literal map rather than assembled, so every key stays
 * greppable and extractable (see `dynamicKeys.test.ts`).
 *
 * Two things ride on the title rather than on the body. **Scope**, because the
 * body's cause clause is the same sentence whichever routes refused. And
 * **permanence**: the two causes are one condition with opposite advice --
 * declared is permanent while the config stands, unreadable can clear on its
 * own -- and a reader who reads only the bold line has to come away with the
 * right one of those. A single shared title put that difference a body-read
 * away, which reads as the same problem twice.
 */
const FILTER_REFUSAL_TITLE_KEYS = {
  declared: {
    both: 'components.gitPanel.filter_refused_title',
    changes: 'components.gitPanel.filter_refused_title_changes',
    history: 'components.gitPanel.filter_refused_title_history',
  },
  unreadable: {
    both: 'components.gitPanel.filter_refused_title_unreadable',
    changes: 'components.gitPanel.filter_refused_title_unreadable_changes',
    history: 'components.gitPanel.filter_refused_title_unreadable_history',
  },
} as const

/**
 * The i18n key for the refusal's title. An unknown or absent cause falls back
 * to the unreadable titles for the same reason the body does: they promise
 * nothing about permanence.
 */
export function gitFilterRefusalTitleKey(
  cause: string | undefined,
  scope: GitFilterRefusalScope,
): string {
  const titles =
    cause === 'declared'
      ? FILTER_REFUSAL_TITLE_KEYS.declared
      : FILTER_REFUSAL_TITLE_KEYS.unreadable
  return titles[scope]
}

/** Which halves a refusal covers, from the two routes' refusal flags. */
export function gitFilterRefusalScope(
  statusRefused: boolean,
  logRefused: boolean,
): GitFilterRefusalScope {
  if (statusRefused && logRefused) return 'both'
  return statusRefused ? 'changes' : 'history'
}
