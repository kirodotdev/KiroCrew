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
 */
export function gitFilterRefusalCopyKey(cause: string | undefined): string {
  return cause === 'declared'
    ? 'components.gitPanel.filter_refused'
    : 'components.gitPanel.filter_refused_unreadable'
}
