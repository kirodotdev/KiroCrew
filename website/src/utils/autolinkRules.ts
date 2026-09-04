/**
 * Bare-token autolink registry.
 *
 * GFM autolinks anything carrying a scheme. It cannot know that in a given
 * organisation a bare token is an address — a ticket key, a change-review id.
 * A downstream edition registers that vocabulary here; the core registers none,
 * so a stock build renders exactly as it would with no seam.
 *   registerAutolinkRules([{
 *     id: 'acme-ticket',
 *     pattern: /\bTICKET-\d+\b/g,
 *     href: 'https://tickets.example.com/{match}',
 *   }])
 *
 * Registered at module load, before `App` mounts. Not reactive.
 */
import { reportSeamCollision } from '../apps/seamCollision'
import { safeHttpUrl } from '../lib/safeUrl'

export interface AutolinkRule {
  /** Stable key for de-dup across re-entrant registration (e.g. HMR). */
  id: string
  /**
   * The token shape. Matched against message PROSE only — never inside code,
   * an existing link, raw HTML, or math (see `remarkAutolinkRules`).
   *
   * Anchor it with `\b` or an explicit boundary class. An unanchored pattern
   * matches inside longer words, which is how `CR-1` ends up linked out of the
   * middle of `INCR-12`.
   */
  pattern: RegExp
  /**
   * Destination template. `{match}` is substituted with the matched token,
   * percent-encoded, so a token cannot introduce a scheme, userinfo, host or
   * query separator.
   *
   * Must be an absolute `http:` or `https:` URL, carrying no userinfo, with
   * `{match}` outside the authority. All three are checked ONCE, at
   * registration: encoding confines a token to a single path, query or fragment
   * segment, so there is no per-match outcome a re-check could differ on.
   */
  href: string
}

const AUTOLINK_RULES: AutolinkRule[] = []

const MATCH_TOKEN = '{match}'

/** A high surrogate with no low after it, or a low with no high before it. */
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g

/** Substitute the matched token into a template, percent-encoded. */
function expand(template: string, match: string): string {
  // A stream cut mid-pair leaves an unpaired surrogate, which the encoder
  // rejects with a URIError; U+FFFD is the platform's own substitute.
  const text = match.replace(LONE_SURROGATE, '\uFFFD')
  return template.split(MATCH_TOKEN).join(encodeURIComponent(text))
}

/**
 * Validate a destination template, returning it or `null` if unusable.
 *
 * Two DIFFERENT canaries must yield the same origin. That is what makes one
 * check sufficient: it rejects a placeholder sitting in the authority, where a
 * token could otherwise steer the host, and leaves only path, query and
 * fragment positions — which percent-encoding cannot escape from.
 */
function normaliseHref(template: string): string | null {
  if (typeof template !== 'string' || !template.includes(MATCH_TOKEN)) return null
  const a = safeHttpUrl(expand(template, 'aaa'))
  const b = safeHttpUrl(expand(template, 'bbb'))
  if (!a || !b) return null
  if (new URL(a).origin !== new URL(b).origin) return null
  return template
}

/** The normalized destination for one matched token. */
export function autolinkHref(rule: AutolinkRule, match: string): string {
  return new URL(expand(rule.href, match)).href
}

/**
 * Normalise a rule's pattern into one that is safe to scan with in a loop:
 * global, non-sticky, and not matching the empty SUBJECT.
 *
 * That last check is a floor, not a guarantee: a pattern can still yield a
 * zero-width match against real content (`/(?=x)/`), which the scan handles by
 * abandoning the rule. Returns `null` when the pattern cannot be made safe, in
 * which case the caller reports a collision-style failure and drops the rule.
 */
function normalisePattern(p: RegExp): RegExp | null {
  if (p.sticky) return null
  // Empty-match test on a deliberately empty subject: a pattern that matches ''
  // matches it here too, and one that cannot will simply miss.
  const probe = new RegExp(p.source, p.flags.replace(/[gy]/g, ''))
  if (probe.test('')) return null
  const flags = p.flags.includes('g') ? p.flags : `${p.flags}g`
  return new RegExp(p.source, flags)
}

/**
 * Register one or more bare-token autolink rules. A duplicate `id` is ignored
 * and reported, so re-entrant registration stays idempotent; an invalid rule is
 * reported and dropped rather than taking the render down with it.
 */
export function registerAutolinkRules(rules: AutolinkRule[]): void {
  for (const r of rules) {
    if (AUTOLINK_RULES.some(existing => existing.id === r.id)) {
      reportSeamCollision('autolinkRules', `rule ${r.id} already registered; ignoring duplicate`)
      continue
    }
    const pattern = normalisePattern(r.pattern)
    if (!pattern) {
      reportSeamCollision(
        'autolinkRules',
        `rule ${r.id} has an unusable pattern (sticky, or matches the empty string); ignoring`,
      )
      continue
    }
    const href = normaliseHref(r.href)
    if (!href) {
      reportSeamCollision(
        'autolinkRules',
        `rule ${r.id} has an unusable href template (needs {match} in the path, query or fragment of an absolute http(s) URL without userinfo); ignoring`,
      )
      continue
    }
    AUTOLINK_RULES.push({ ...r, pattern, href })
  }
}

/** All registered rules, in registration order (earlier rules win an overlap). */
export function getAutolinkRules(): readonly AutolinkRule[] {
  return AUTOLINK_RULES
}

/** Drop every registered rule. Test-only; the app never unregisters. */
export function resetAutolinkRulesForTest(): void {
  AUTOLINK_RULES.length = 0
}
